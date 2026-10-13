#include "esp_camera.h"
#include <WiFi.h>

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
const char* SEC          = "smarthome/device/cam01/security";
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

String state = "NORMAL";
bool quarantine = false;

unsigned long lastDiscovery = 0;
unsigned long lastFrame = 0;
unsigned long lastMqttRetry = 0;

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
    "\",\"config_topic\":\"smarthome/device/cam01/config\"" +
    ",\"security_topic\":\"" + String(SEC) +
    "\"}";

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
    ",\"security_state\":\"" + state +
    "\",\"quarantined\":" + (quarantine ? String("true") : String("false")) +
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
// MQTT CALLBACK
// ============================================================

void cb(char* topic, byte* payload, unsigned len) {
  String m;
  m.reserve(len + 1);

  for (unsigned i = 0; i < len; i++) {
    m += (char)payload[i];
  }

  Serial.print("[MQTT RX] ");
  Serial.print(topic);
  Serial.print(" -> ");
  Serial.println(m);

  if (m.indexOf("\"action\":\"QUARANTINE\"") >= 0) {
    quarantine = true;
    state = "QUARANTINED";
    status();
  }
  else if (m.indexOf("\"action\":\"RELEASE\"") >= 0) {
    quarantine = false;
    state = "NORMAL";
    status();
  }
  else if (m.indexOf("\"state\":\"SUSPICIOUS\"") >= 0) {
    state = "SUSPICIOUS";
    status();
  }
  else if (m.indexOf("\"state\":\"ATTACKED\"") >= 0) {
    state = "ATTACKED";
    status();
  }
  else if (m.indexOf("\"state\":\"NORMAL\"") >= 0) {
    state = "NORMAL";
    status();
  }
}

// ============================================================
// WIFI
// ============================================================

void wifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("[WIFI] Connecting");

  unsigned long start = millis();

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print('.');

    if (millis() - start > 20000) {
      Serial.println();
      Serial.println("[WIFI] Connection timeout");
      return;
    }
  }

  Serial.println();
  Serial.print("[WIFI] Connected. IP = ");
  Serial.println(WiFi.localIP());
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

  if (mqtt.connect(ID)) {
    Serial.println("[MQTT] CONNECTED");

    bool subOK = mqtt.subscribe(SEC);

    if (subOK) {
      Serial.print("[MQTT] SUBSCRIBED: ");
      Serial.println(SEC);
    }
    else {
      Serial.println("[MQTT] SUBSCRIBE FAILED");
    }

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
      ",\"security_state\":\"" + state +
      "\",\"quarantined\":" + (quarantine ? String("true") : String("false")) +
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

void newStream() {
  if (sc && sc.connected()) {
    return;
  }

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

  sc.println("HTTP/1.1 200 OK");
  sc.println("Content-Type: multipart/x-mixed-replace; boundary=frame");
  sc.println("Cache-Control: no-cache");
  sc.println("Access-Control-Allow-Origin: *");
  sc.println();

  Serial.println("[STREAM] Client connected");
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

  sc.print("--frame\r\n");
  sc.print("Content-Type: image/jpeg\r\n");
  sc.print("Content-Length: ");
  sc.print(len);
  sc.print("\r\n\r\n");

  size_t written = sc.write(fb->buf, len);
  sc.print("\r\n");

  esp_camera_fb_return(fb);
  capturing = false;

  if (written != len) {
    Serial.println("[STREAM] Client disconnected");
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
  mqtt.setCallback(cb);

  // Quan trọng: tăng packet buffer cho discovery/status JSON.
  mqtt.setBufferSize(2048);

  Serial.print("[MQTT] Buffer size = ");
  Serial.println(2048);

  api.begin();
  stream.begin();

  Serial.println("[HTTP] Status server :80 started");
  Serial.println("[HTTP] Stream server :81 started");
}

// ============================================================
// LOOP
// ============================================================

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    wifi();
  }

  if (!mqtt.connected()) {
    mc();
  }

  mqtt.loop();

  apiHandler();
  newStream();

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
