#include "esp_camera.h"
#include <WiFi.h>
#include <mqtt_secrets.h>

// PubSubClient mặc định chỉ cho packet nhỏ. Camera gửi JSON discovery/status
// dài hơn nên tăng buffer trước khi include thư viện.
#define MQTT_MAX_PACKET_SIZE 2048
#include <PubSubClient.h>

// ============================================================
// ESP32-CAM AI-THINKER / OV3660
// ============================================================

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5

#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// ============================================================
// WIFI / MQTT CONFIG
// ============================================================

const char* WIFI_SSID     = "17.17 2.4g";
const char* WIFI_PASSWORD = "Giahuy1717";

const char* MQTT_SERVER = "192.168.1.130";
const int   MQTT_PORT   = 1883;

const char* ID = "ESP32-CAM-01";

const char* TOPIC_STATUS = "smarthome/device/cam01/status";
const char* ANNOUNCE     = "smarthome/device/announce";
const char* REGISTER     = "smarthome/device/register";

// ============================================================
// GLOBALS
// ============================================================

WiFiClient net;
PubSubClient mqtt(net);

WiFiServer api(80);
WiFiServer stream(81);
WiFiClient sc;


unsigned long lastDiscovery = 0;
unsigned long lastFrame = 0;
unsigned long lastMqttRetry = 0;
unsigned long wifiAttemptStarted = 0;
unsigned long lastWifiRetry = 0;
unsigned long wifiRetryDelay = 1000;
bool wifiConnecting = false;
bool wifiWasConnected = false;

bool capturing = false;

// ============================================================
// HELPERS
// ============================================================

String camIp() {
  return WiFi.localIP().toString();
}

String streamUrl() {
  return "http://" + camIp() + ":81/stream";
}

String statusUrl() {
  return "http://" + camIp() + "/status";
}

void printPublishResult(const char* label, bool result, size_t payloadLen) {
  Serial.print("[MQTT] ");
  Serial.print(label);
  Serial.print(" = ");
  Serial.print(result ? "OK" : "FAILED");
  Serial.print(" | payload=");
  Serial.print(payloadLen);
  Serial.print(" bytes | mqtt=");
  Serial.println(mqtt.connected() ? "CONNECTED" : "DISCONNECTED");
}

// ============================================================
// MQTT DISCOVERY
// ============================================================

void discovery() {
  if (!mqtt.connected()) {
    Serial.println("[MQTT] Discovery skipped: MQTT disconnected");
    return;
  }

  String ip = camIp();

  String p =
    "{\"device_id\":\"" + String(ID) +
    "\",\"type\":\"camera\"" +
    ",\"device_type\":\"camera\"" +
    ",\"name\":\"" + String(ID) +
    "\",\"room\":\"\"" +
    ",\"online\":true" +
    ",\"ip\":\"" + ip +
    "\",\"http_port\":80" +
    ",\"stream_port\":81" +
    ",\"stream\":\"" + streamUrl() +
    "\",\"status_url\":\"" + statusUrl() +
    "\",\"config_topic\":\"smarthome/device/cam01/config\"}";

  Serial.print("[MQTT] Discovery payload length = ");
  Serial.println(p.length());

  bool r1 = mqtt.publish(REGISTER, p.c_str(), false);
  bool r2 = mqtt.publish(ANNOUNCE, p.c_str(), false);

  printPublishResult("REGISTER", r1, p.length());
  printPublishResult("ANNOUNCE", r2, p.length());
}

// ============================================================
// MQTT STATUS
// ============================================================

void status() {
  if (!mqtt.connected()) {
    Serial.println("[MQTT] Status skipped: MQTT disconnected");
    return;
  }

  String p =
    "{\"device_id\":\"" + String(ID) +
    "\",\"device_type\":\"camera\"" +
    ",\"online\":true" +
    ",\"ip\":\"" + camIp() +
    "\",\"http_port\":80" +
    ",\"stream_port\":81" +
    ",\"stream\":\"" + streamUrl() +
    "\",\"status_url\":\"" + statusUrl() +
    "\"}";

  Serial.print("[MQTT] Status payload length = ");
  Serial.println(p.length());

  // Retained: AppDemo có thể nhận trạng thái camera ngay khi subscribe.
  bool r = mqtt.publish(TOPIC_STATUS, p.c_str(), true);
  printPublishResult("STATUS", r, p.length());
}

// ============================================================
// WIFI
// ============================================================

void wifi() {
  const unsigned long now = millis();
  const bool connected = WiFi.status() == WL_CONNECTED;

  if (connected) {
    wifiConnecting = false;
    wifiRetryDelay = 1000;
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      Serial.print("[WIFI] Connected. IP = ");
      Serial.println(WiFi.localIP());
      api.begin();
      stream.begin();
      Serial.println("[HTTP] Status server :80 started");
      Serial.println("[HTTP] Stream server :81 started");
      lastMqttRetry = 0;
    }
    return;
  }

  if (wifiWasConnected) {
    wifiWasConnected = false;
    wifiConnecting = false;
    Serial.println("[WIFI] Disconnected; closing stale clients");
    mqtt.disconnect();
    sc.stop();
  }

  if (wifiConnecting && now - wifiAttemptStarted < 20000) return;
  if (wifiConnecting) {
    Serial.println("[WIFI] Connection attempt timed out");
    wifiConnecting = false;
  }
  if (now - lastWifiRetry < wifiRetryDelay) return;

  lastWifiRetry = now;
  wifiAttemptStarted = now;
  wifiConnecting = true;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  Serial.println("[WIFI] Starting connection attempt");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiRetryDelay = min(wifiRetryDelay * 2, 30000UL);
}

// ============================================================
// MQTT CONNECTION
// ============================================================

void mc() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (mqtt.connected()) {
    return;
  }

  // Không reconnect quá dồn dập.
  if (millis() - lastMqttRetry < 3000) {
    return;
  }

  lastMqttRetry = millis();

  Serial.print("[MQTT] Connecting to ");
  Serial.print(MQTT_SERVER);
  Serial.print(":");
  Serial.println(MQTT_PORT);

  if (mqtt.connect(ID, MQTT_USERNAME, MQTT_PASSWORD)) {
    Serial.println("[MQTT] CONNECTED");

    // Gửi ngay khi vừa kết nối.
    discovery();
    status();
  }
  else {
    Serial.print("[MQTT] FAILED, state=");
    Serial.println(mqtt.state());
  }
}

// ============================================================
// CAMERA INIT
// ============================================================

void camera() {
  camera_config_t c = {};

  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;

  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;

  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;

  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    Serial.println("[CAMERA] PSRAM detected");
    Serial.println("[CAMERA] Resolution: 640x480 (HD), target 30 FPS");

    // 720p HD (640x480).
    c.frame_size = FRAMESIZE_VGA;
    c.jpeg_quality = 12;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }
  else {
    Serial.println("[CAMERA] PSRAM not detected");
    Serial.println("[CAMERA] Resolution: 640x480 (HD), target 30 FPS");

    c.frame_size = FRAMESIZE_VGA;
    c.jpeg_quality = 15;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  Serial.println("[CAMERA] Initializing...");

  esp_err_t err = esp_camera_init(&c);

  if (err != ESP_OK) {
    Serial.printf("[CAMERA] Init failed: 0x%x\n", err);

    while (true) {
      delay(1000);
    }
  }

  sensor_t* s = esp_camera_sensor_get();

  if (s != nullptr) {
    Serial.printf("[CAMERA] Sensor PID: 0x%04X\n", s->id.PID);
  }

  Serial.println("[CAMERA] initialized OK");
}

// ============================================================
// HTTP STATUS API
// ============================================================

void apiHandler() {
  WiFiClient c = api.available();

  if (!c) {
    return;
  }

  c.setTimeout(100);

  String r = c.readStringUntil('\r');
  c.readStringUntil('\n');

  if (r.indexOf("GET /status") >= 0) {
    String b =
      String("{\"device\":\"ESP32-CAM-01\"") +
      ",\"online\":true" +
      ",\"ip\":\"" + WiFi.localIP().toString() +
      "\",\"stream\":\"http://" + WiFi.localIP().toString() + ":81/stream\"}";

    c.println("HTTP/1.1 200 OK");
    c.println("Content-Type: application/json");
    c.println("Connection: close");
    c.println();
    c.println(b);
  }
  else {
    c.println("HTTP/1.1 404 Not Found");
    c.println("Connection: close");
    c.println();
  }

  c.stop();
}

// ============================================================
// STREAM CONNECTION
// ============================================================

bool writeStreamData(const uint8_t* data, size_t length, size_t& sent);

void newStream() {
  if (sc && sc.connected()) {
    return;
  }
  if (sc) sc.stop();

  WiFiClient c = stream.available();

  if (!c) {
    return;
  }

  c.setTimeout(1000);

  String r = c.readStringUntil('\r');
  c.readStringUntil('\n');

  if (r.indexOf("GET /stream") < 0) {
    c.stop();
    return;
  }

  sc = c;
  static const char response[] =
      "HTTP/1.1 200 OK\r\n"
      "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
      "Cache-Control: no-cache\r\n"
      "Access-Control-Allow-Origin: *\r\n\r\n";
  size_t responseSent = 0;
  if (!writeStreamData(reinterpret_cast<const uint8_t*>(response),
                       sizeof(response) - 1, responseSent)) {
    Serial.printf("[STREAM] Response send failed (%u/%u bytes)\n",
                  static_cast<unsigned>(responseSent),
                  static_cast<unsigned>(sizeof(response) - 1));
    sc.stop();
    return;
  }

  Serial.println("[STREAM] Client connected");
}

bool writeStreamData(const uint8_t* data, size_t length, size_t& sent) {
  sent = 0;
  unsigned long lastProgress = millis();

  while (sent < length) {
    size_t written = sc.write(data + sent, length - sent);
    if (written > 0) {
      sent += written;
      lastProgress = millis();
      continue;
    }

    if (!sc.connected() || millis() - lastProgress >= 1500) {
      return false;
    }
    delay(1);
  }

  return true;
}

// ============================================================
// SEND CAMERA FRAME
// ============================================================

void frame() {
  if (capturing) {
    return;
  }

  if (!sc || !sc.connected()) {
    return;
  }

  capturing = true;

  camera_fb_t* fb = esp_camera_fb_get();

  if (fb == nullptr) {
    Serial.println("[CAMERA] Frame capture failed");
    capturing = false;
    return;
  }

  size_t len = fb->len;
  char header[96];
  int headerLength = snprintf(header, sizeof(header),
                              "--frame\r\nContent-Type: image/jpeg\r\n"
                              "Content-Length: %u\r\n\r\n",
                              static_cast<unsigned>(len));
  size_t headerSent = 0;
  size_t frameSent = 0;
  static const uint8_t frameEnd[] = "\r\n";
  size_t frameEndSent = 0;

  bool sentCompletely = headerLength > 0 &&
      static_cast<size_t>(headerLength) < sizeof(header) &&
      writeStreamData(reinterpret_cast<const uint8_t*>(header),
                      static_cast<size_t>(headerLength), headerSent) &&
      writeStreamData(fb->buf, len, frameSent) &&
      writeStreamData(frameEnd, sizeof(frameEnd) - 1, frameEndSent);

  esp_camera_fb_return(fb);
  capturing = false;

  if (!sentCompletely) {
    Serial.printf("[STREAM] Send failed (header=%u/%d, frame=%u/%u, end=%u/2); closing client\n",
                  static_cast<unsigned>(headerSent), headerLength,
                  static_cast<unsigned>(frameSent), static_cast<unsigned>(len),
                  static_cast<unsigned>(frameEndSent));
    sc.stop();
  }
}

// ============================================================
// SETUP
// ============================================================

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("==============================");
  Serial.println("ESP32-CAM SMART HOME");
  Serial.println("==============================");

  Serial.println("[1] Starting camera...");
  camera();

  Serial.println("[2] Camera OK");
  Serial.println("[3] Starting WiFi...");
  wifi();

  // MQTT configuration.
  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setKeepAlive(30);
  mqtt.setSocketTimeout(5);

  // Quan trọng: tăng packet buffer cho discovery/status JSON.
  mqtt.setBufferSize(2048);

  Serial.print("[MQTT] Buffer size = ");
  Serial.println(2048);

}

// ============================================================
// LOOP
// ============================================================

void loop() {
  wifi();

  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) {
    mc();
  }

  if (WiFi.status() == WL_CONNECTED && mqtt.connected()) mqtt.loop();

  if (WiFi.status() == WL_CONNECTED) {
    apiHandler();
    newStream();
  }

  // Target 30 FPS (~33 ms/frame).
  // FPS thực tế còn phụ thuộc OV3660, PSRAM, JPEG size và Wi-Fi throughput.
  if (sc && sc.connected() && millis() - lastFrame >= 33) {
    lastFrame = millis();
    frame();
  }

  // Discovery/status định kỳ mỗi 30 giây.
  if (mqtt.connected() && millis() - lastDiscovery > 30000) {
    lastDiscovery = millis();
    discovery();
    status();
  }

  delay(2);
}
