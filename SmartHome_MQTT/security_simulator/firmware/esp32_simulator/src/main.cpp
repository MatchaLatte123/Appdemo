#include <Arduino.h>
#include <atomic>
#include <HTTPClient.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <esp_task_wdt.h>

#if __has_include("simulator_config.h")
#include "simulator_config.h"
#else
#include "config.example.h"
#endif

namespace {
constexpr unsigned Protocol = 1;
constexpr unsigned MaxOperations = 8;
constexpr uint32_t MaxRunMs = 45000;
constexpr uint32_t IntervalMs = 3000;
constexpr uint32_t RestMs = 15000;
constexpr uint8_t StopPin = 27;

enum class Mode { Idle, Mqtt, Http, Reconnect, ProtocolTest, Recovery };
const char* const Names[] = {
    "IDLE", "NORMAL_MQTT", "NORMAL_HTTP", "NORMAL_RECONNECT", "BOUNDED_PROTOCOL_TEST", "RECOVERY"};
struct Job { Mode mode; unsigned sequence; char id[14]; };
struct Result { bool success; int httpStatus; };

class BoundedClient : public WiFiClient {
public:
    using WiFiClient::connect;
    int connect(IPAddress ip, uint16_t port) override {
        return WiFiClient::connect(ip, port, 850);
    }
};

Preferences preferences;
QueueHandle_t jobs, results;
Mode mode = Mode::Idle;
IPAddress broker, video;
bool brokerReady, videoReady, nvsReady, busy, watchdog;
uint32_t bootId, started, nextOperation, restUntil;
unsigned attempts, successes, failures;
int httpStatus;
char deviceId[14]{};
char line[64]{};
unsigned lineSize;
bool lineOverflow;
const char* safety = "READY";
std::atomic<bool> stopped{true};

bool reached(uint32_t now, uint32_t deadline) {
    return static_cast<int32_t>(now - deadline) >= 0;
}

bool privateIp(const char* text, IPAddress& address) {
    return address.fromString(text) &&
           (address[0] == 10 || (address[0] == 172 && address[1] >= 16 && address[1] <= 31)
            || (address[0] == 192 && address[1] == 168));
}

bool validId(const char* id) {
    return strlen(id) == 12 && strncmp(id, "ESP32-SIM-", 10) == 0 &&
           id[10] >= '0' && id[10] <= '9' && id[11] >= '0' && id[11] <= '9' &&
           (id[10] != '0' || id[11] != '0');
}

void finish(const char* reason) {
    if (mode != Mode::Idle) restUntil = millis() + RestMs;
    stopped = true;
    mode = Mode::Idle;
    safety = reason;
}

void emit(const char* result) {
    Serial.printf("{\"kind\":\"SMART_HOME_SIM\",\"protocol\":%u,\"version\":\"1.0.0\","
                  "\"device_id\":\"%s\",\"boot_id\":\"%08lx\",\"wifi\":%s,\"ip\":\"%s\","
                  "\"mode\":\"%s\",\"attempts\":%u,\"successes\":%u,\"failures\":%u,"
                  "\"http_status\":%d,\"busy\":%s,\"safety\":\"%s\",\"result\":\"%s\"}\n",
                  Protocol, deviceId, static_cast<unsigned long>(bootId),
                  WiFi.status() == WL_CONNECTED ? "true" : "false", WiFi.localIP().toString().c_str(),
                  Names[static_cast<int>(mode)], attempts, successes, failures, httpStatus,
                  busy ? "true" : "false", safety, result);
}

Result httpRequest() {
    BoundedClient transport;
    HTTPClient http;
    http.setConnectTimeout(850);
    http.setTimeout(850);
    http.setReuse(false);
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    auto url = String("http://") + video.toString() + ":" + SIM_VIDEO_PORT + "/health";
    if (!http.begin(transport, url)) return {false, -1};
    int code = http.GET();
    http.end();
    return {code >= 200 && code < 300, code};
}

Result mqttRequest(const Job& job, BoundedClient& tcp, PubSubClient& mqtt) {
    if (!mqtt.connected()) {
        if (!mqtt.connect(job.id, SIM_MQTT_USERNAME, SIM_MQTT_PASSWORD)) {
            tcp.stop();
            return {false, 0};
        }
    }
    char topic[70];
    snprintf(topic, sizeof(topic), "smarthome/simulator/%s/telemetry", job.id);
    char payload[100];
    snprintf(payload, sizeof(payload), "{\"device_id\":\"%s\",\"sample\":%u,\"test\":true}", job.id, job.sequence);
    bool sent = mqtt.publish(topic, payload, false);
    if (job.mode != Mode::Mqtt || !sent || job.sequence == MaxOperations) {
        mqtt.disconnect();
        tcp.stop();
    }
    return {sent, 0};
}

void networkWorker(void*) {
    if (esp_task_wdt_add(nullptr) != ESP_OK) vTaskDelete(nullptr);
    BoundedClient tcp;
    PubSubClient mqtt(tcp);
    mqtt.setServer(broker, SIM_BROKER_PORT);
    mqtt.setSocketTimeout(1);
    mqtt.setKeepAlive(10);
    Job job{};
    while (true) {
        esp_task_wdt_reset();
        if (xQueueReceive(jobs, &job, pdMS_TO_TICKS(100)) == pdTRUE) {
            Result result{false, 0};
            if (!stopped && WiFi.status() == WL_CONNECTED) {
                result = job.mode == Mode::Http ? httpRequest() : mqttRequest(job, tcp, mqtt);
            }
            xQueueSend(results, &result, portMAX_DELAY);
        } else if (stopped && tcp.connected()) {
            mqtt.disconnect();
            tcp.stop();
        }
    }
}

void handle(const char* command) {
    if (strcmp(command, "HELLO") == 0 || strcmp(command, "STATUS") == 0) {
        emit("OK");
        return;
    }
    if (strcmp(command, "STOP") == 0) {
        finish("STOPPED");
        emit("OK");
        return;
    }
    if (strcmp(command, "RESET") == 0 && mode == Mode::Idle && !busy) {
        attempts = successes = failures = 0;
        httpStatus = 0;
        emit("OK");
        return;
    }
    if (strncmp(command, "SET_ID ", 7) == 0) {
        const char* id = command + 7;
        if (mode != Mode::Idle || busy || !nvsReady || !validId(id) || preferences.putString("id", id) != strlen(id)) {
            emit("REJECTED");
            return;
        }
        memcpy(deviceId, id, strlen(id) + 1);
        emit("OK");
        return;
    }
    Mode requested = Mode::Idle;
    for (int i = 1; i <= static_cast<int>(Mode::Recovery); ++i) {
        if (strncmp(command, "START ", 6) == 0 && strcmp(command + 6, Names[i]) == 0) {
            requested = static_cast<Mode>(i);
        }
    }
    if (requested == Mode::Idle || mode != Mode::Idle || busy || !jobs || !results || !watchdog ||
        !validId(deviceId) || !reached(millis(), restUntil) || digitalRead(StopPin) == LOW ||
        (requested != Mode::Recovery && WiFi.status() != WL_CONNECTED) ||
        (requested == Mode::Http && !videoReady) ||
        (requested != Mode::Http && requested != Mode::Recovery && !brokerReady)) {
        emit("REJECTED");
        return;
    }
    stopped = false;
    mode = requested;
    attempts = successes = failures = 0;
    httpStatus = 0;
    started = nextOperation = millis();
    safety = "RUNNING";
    emit("OK");
}

void readSerial() {
    // Bound work per loop even when the host sends a burst of bytes.
    for (unsigned count = 0; count < 96 && Serial.available(); ++count) {
        char ch = static_cast<char>(Serial.read());
        if (ch == '\n') {
            line[lineSize] = 0;
            if (lineOverflow) emit("REJECTED");
            else if (lineSize) handle(line);
            lineOverflow = false;
            lineSize = 0;
        } else if (ch != '\r' && !lineOverflow) {
            if (lineSize + 1 < sizeof(line) && ch >= 32 && ch <= 126) line[lineSize++] = ch;
            else lineOverflow = true;
        }
    }
}

void tick() {
    const uint32_t now = millis();
    if (digitalRead(StopPin) == LOW) finish("HARD_STOP");
    Result result{};
    if (busy && xQueueReceive(results, &result, 0) == pdTRUE) {
        busy = false;
        httpStatus = result.httpStatus;
        if (result.success) ++successes;
        else {
            ++failures;
            if (mode != Mode::Idle) finish("BACKOFF");
        }
    }
    if (mode == Mode::Idle) return;
    if (mode == Mode::Recovery) {
        if (now - started >= RestMs) finish(WiFi.status() == WL_CONNECTED ? "RECOVERED" : "WIFI_DOWN");
        return;
    }
    if (now - started >= MaxRunMs || WiFi.status() != WL_CONNECTED) {
        finish(WiFi.status() == WL_CONNECTED ? "BUDGET_EXHAUSTED" : "WIFI_DOWN");
        return;
    }
    if (busy || !reached(now, nextOperation)) return;
    const unsigned limit = mode == Mode::ProtocolTest ? 2 : mode == Mode::Reconnect ? 3 : MaxOperations;
    if (attempts >= limit) {
        finish("COMPLETE");
        return;
    }
    Job job{mode, attempts + 1, {}};
    memcpy(job.id, deviceId, sizeof(deviceId));
    if (xQueueSend(jobs, &job, 0) != pdTRUE) {
        finish("BACKOFF");
        return;
    }
    ++attempts;
    busy = true;
    nextOperation = now + IntervalMs;
}
} // namespace

void setup() {
    Serial.begin(115200);
    pinMode(StopPin, INPUT_PULLUP);
    bootId = esp_random();
    nvsReady = preferences.begin("iot-sim", false);
    if (nvsReady && preferences.getString("id", deviceId, sizeof(deviceId)) && !validId(deviceId)) deviceId[0] = 0;
    brokerReady = privateIp(SIM_BROKER_IPV4, broker) && SIM_BROKER_PORT > 0 && SIM_BROKER_PORT <= 65535 &&
                  SIM_MQTT_USERNAME[0] && SIM_MQTT_PASSWORD[0];
    videoReady = privateIp(SIM_VIDEO_IPV4, video) && SIM_VIDEO_PORT > 0 && SIM_VIDEO_PORT <= 65535;
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    if (SIM_WIFI_SSID[0]) WiFi.begin(SIM_WIFI_SSID, SIM_WIFI_PASSWORD);
    watchdog = esp_task_wdt_init(6, true) == ESP_OK &&
               (esp_task_wdt_status(nullptr) == ESP_OK || esp_task_wdt_add(nullptr) == ESP_OK);
    jobs = xQueueCreate(1, sizeof(Job));
    results = xQueueCreate(1, sizeof(Result));
    if (!watchdog || !jobs || !results || xTaskCreate(networkWorker, "sim-net", 6144, nullptr, 1, nullptr) != pdPASS) {
        jobs = nullptr;
        safety = "WORKER_ERROR";
    }
}

void loop() {
    readSerial();
    tick();
    if (watchdog) esp_task_wdt_reset();
    delay(5);
}
