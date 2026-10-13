#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>

// Forward declarations
void publishStatus();
void publishTelemetry();
void publishRegister();
void publishConfigAck(const char* status);

// =====================================================
// WIFI
// =====================================================
const char* WIFI_SSID = "17.17 2.4g";
const char* WIFI_PASSWORD = "Giahuy1717";

// =====================================================
// MQTT
// =====================================================
const char* MQTT_SERVER = "192.168.1.130";
const uint16_t MQTT_PORT = 1883;
const char* DEVICE_ID = "ESP32-RELAY-01";
const char* DEVICE_TYPE = "relay";

const char* TOPIC_REGISTER = "smarthome/device/register";
const char* TOPIC_ANNOUNCE = "smarthome/device/announce";
const char* TOPIC_STATUS = "smarthome/device/relay01/status";
const char* TOPIC_TELEMETRY = "smarthome/device/relay01/telemetry";
const char* TOPIC_CONTROL = "smarthome/device/relay01/control";
const char* TOPIC_SECURITY = "smarthome/security/relay01";
const char* TOPIC_CONFIG = "smarthome/device/relay01/config";
const char* TOPIC_CONFIG_ACK = "smarthome/device/relay01/config_ack";

// =====================================================
// 4-CHANNEL RELAY
// =====================================================
// CH1/CH2 keep the pins from the original RelayWebTest.ino.
// CH3/CH4 are added on safe general-purpose GPIOs.
#define RELAY1_PIN 21
#define RELAY2_PIN 22
#define RELAY3_PIN 23
#define RELAY4_PIN 19

#define RELAY_ON HIGH
#define RELAY_OFF LOW

const uint8_t RELAY_PINS[4] = {
  RELAY1_PIN, RELAY2_PIN, RELAY3_PIN, RELAY4_PIN
};

// =====================================================
// MQTT / NVS
// =====================================================
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
Preferences prefs;

// =====================================================
// DEVICE STATE
// =====================================================
bool relayState[4] = {false, false, false, false};
String securityState = "NORMAL";
bool quarantined = false;

// =====================================================
// PROFILE
// =====================================================
String deviceName = "ESP32-RELAY-01";
String deviceRoom = "";
int profileVersion = 0;

struct ChannelProfile {
  String name;
  String room;
  String mode;          // manual | schedule
  bool defaultState;
  String scheduleOn;
  String scheduleOff;
  uint8_t scheduleDayMask;
};

ChannelProfile channels[4];

unsigned long lastMqttReconnect = 0;
unsigned long lastTelemetry = 0;
unsigned long lastAnnounce = 0;
unsigned long lastScenarioCheck = 0;

const unsigned long MQTT_RECONNECT_INTERVAL = 5000;
const unsigned long TELEMETRY_INTERVAL = 10000;
const unsigned long ANNOUNCE_INTERVAL = 30000;
const unsigned long SCENARIO_INTERVAL = 5000;

// =====================================================
// DEFAULT PROFILE
// =====================================================
void setDefaultChannel(int index) {
  channels[index].name = "Relay " + String(index + 1);
  channels[index].room = "";
  channels[index].mode = "manual";
  channels[index].defaultState = false;
  channels[index].scheduleOn = "08:00";
  channels[index].scheduleOff = "22:00";
  channels[index].scheduleDayMask = 0x7F;
}

void setDefaultProfile() {
  deviceName = DEVICE_ID;
  deviceRoom = "";
  profileVersion = 0;
  securityState = "NORMAL";
  quarantined = false;
  for (int i = 0; i < 4; i++) {
    setDefaultChannel(i);
    relayState[i] = false;
  }
}

// =====================================================
// RELAY OUTPUT
// =====================================================
void setRelayState(int channel, bool state, bool publish = true) {
  if (channel < 1 || channel > 4) return;

  int index = channel - 1;
  bool actual = quarantined ? false : state;

  digitalWrite(RELAY_PINS[index], actual ? RELAY_ON : RELAY_OFF);
  relayState[index] = actual;

  Serial.printf("[RELAY] CH%d -> %s\n", channel, actual ? "ON" : "OFF");
}

void setAllRelayState(bool state) {
  for (int i = 0; i < 4; i++) {
    setRelayState(i + 1, state, false);
  }
}

// =====================================================
// HH:MM / SCHEDULE
// =====================================================
bool parseHHMM(const String& value, int& minutes) {
  int colon = value.indexOf(':');
  if (colon < 1) return false;

  int h = value.substring(0, colon).toInt();
  int m = value.substring(colon + 1).toInt();

  if (h < 0 || h > 23 || m < 0 || m > 59) return false;

  minutes = h * 60 + m;
  return true;
}

bool getLocalClock(struct tm& timeinfo) {
  if (!getLocalTime(&timeinfo, 1000)) {
    return false;
  }
  return timeinfo.tm_year >= (2020 - 1900);
}

bool isWithinSchedule(const String& scheduleOn, const String& scheduleOff, uint8_t scheduleDayMask) {
  struct tm timeinfo;
  if (!getLocalClock(timeinfo)) return false;

  int nowMin = timeinfo.tm_hour * 60 + timeinfo.tm_min;
  int onMin;
  int offMin;

  if (!parseHHMM(scheduleOn, onMin) || !parseHHMM(scheduleOff, offMin)) {
    Serial.printf("[SCHEDULE] Invalid time %s -> %s\n", scheduleOn.c_str(), scheduleOff.c_str());
    return false;
  }

  // The selected weekday is the start day for overnight windows.
  int dayBit = (timeinfo.tm_wday + 6) % 7; // Monday=bit 0
  if (onMin > offMin && nowMin < offMin) dayBit = (dayBit + 6) % 7;
  if (!(scheduleDayMask & (1u << dayBit))) return false;

  if (onMin == offMin) return true;
  if (onMin < offMin) return nowMin >= onMin && nowMin < offMin;
  return nowMin >= onMin || nowMin < offMin;
}

// =====================================================
// SAVE / LOAD PROFILE
// =====================================================
void saveProfile() {
  JsonDocument d;
  d["device_id"] = DEVICE_ID;
  d["type"] = DEVICE_TYPE;
  d["name"] = deviceName;
  d["room"] = deviceRoom;
  d["version"] = profileVersion;

  JsonArray arr = d["channels"].to<JsonArray>();
  for (int i = 0; i < 4; i++) {
    JsonObject c = arr.add<JsonObject>();
    c["id"] = i + 1;
    c["name"] = channels[i].name;
    c["room"] = channels[i].room;
    c["mode"] = channels[i].mode;
    c["default_state"] = channels[i].defaultState;
    c["schedule_on"] = channels[i].scheduleOn;
    c["schedule_off"] = channels[i].scheduleOff;
    JsonArray days = c["schedule_days"].to<JsonArray>();
    const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    for (int day = 0; day < 7; day++) if (channels[i].scheduleDayMask & (1u << day)) days.add(names[day]);
  }

  String json;
  serializeJson(d, json);

  if (!prefs.begin("relay4", false)) {
    Serial.println("[PROFILE] NVS open failed");
    return;
  }
  prefs.putString("profile", json);
  prefs.end();
  Serial.printf("[PROFILE] Saved to NVS version=%d\n", profileVersion);
}

void loadProfile() {
  setDefaultProfile();

  if (!prefs.begin("relay4", false)) {
    Serial.println("[PROFILE] NVS unavailable -> defaults");
    return;
  }

  String json = prefs.getString("profile", "");
  prefs.end();

  if (json.isEmpty()) {
    Serial.println("[PROFILE] No saved profile -> defaults");
    return;
  }

  JsonDocument d;
  DeserializationError error = deserializeJson(d, json);
  if (error) {
    Serial.printf("[PROFILE] JSON ERROR: %s -> defaults\n", error.c_str());
    return;
  }

  deviceName = d["name"] | DEVICE_ID;
  deviceRoom = d["room"] | "";
  profileVersion = d["version"] | 0;

  JsonArray arr = d["channels"].as<JsonArray>();
  if (!arr.isNull()) {
    for (int i = 0; i < 4; i++) {
      setDefaultChannel(i);
      if (i >= (int)arr.size()) continue;
      JsonObject c = arr[i];
      channels[i].name = c["name"] | channels[i].name;
      channels[i].room = c["room"] | "";
      channels[i].mode = c["mode"] | "manual";
      channels[i].defaultState = c["default_state"] | false;
      channels[i].scheduleOn = c["schedule_on"] | "08:00";
      channels[i].scheduleOff = c["schedule_off"] | "22:00";
      if (c["schedule_days"].is<JsonArray>()) {
        channels[i].scheduleDayMask = 0;
        JsonArray days = c["schedule_days"].as<JsonArray>();
        const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
        for (JsonVariant day : days) for (int d = 0; d < 7; d++) if (day == names[d]) channels[i].scheduleDayMask |= (1u << d);
      }
      if (channels[i].mode != "manual" && channels[i].mode != "schedule") {
        channels[i].mode = "manual";
      }
    }
  }

  Serial.printf("[PROFILE] %s | room=%s | ver=%d\n", deviceName.c_str(), deviceRoom.c_str(), profileVersion);
  for (int i = 0; i < 4; i++) {
    Serial.printf("  CH%d %s | room=%s | mode=%s | default=%s | %s-%s\n",
      i + 1,
      channels[i].name.c_str(),
      channels[i].room.c_str(),
      channels[i].mode.c_str(),
      channels[i].defaultState ? "ON" : "OFF",
      channels[i].scheduleOn.c_str(),
      channels[i].scheduleOff.c_str());
  }
}

void applyInitialProfile() {
  for (int i = 0; i < 4; i++) {
    relayState[i] = false;
  }

  if (quarantined) {
    setAllRelayState(false);
    return;
  }

  for (int i = 0; i < 4; i++) {
    if (channels[i].mode == "manual") {
      setRelayState(i + 1, channels[i].defaultState, false);
    }
  }
}

void resetProfileToDefault() {
  Serial.println("[PROFILE] RESET requested -> restoring default relay profile");
  setDefaultProfile();
  saveProfile();
  setAllRelayState(false);
  publishStatus();
  publishRegister();
  publishConfigAck("RESET_APPLIED");
  Serial.println("[PROFILE] Default 4-channel relay profile restored");
}

// =====================================================
// STATUS / TELEMETRY / REGISTER
// =====================================================
void publishStatus() {
  if (!mqttClient.connected()) return;

  JsonDocument d;
  d["device_id"] = DEVICE_ID;
  d["device_type"] = DEVICE_TYPE;
  d["name"] = deviceName;
  d["room"] = deviceRoom;
  d["online"] = true;
  d["profile_version"] = profileVersion;
  d["security_state"] = securityState;
  d["quarantined"] = quarantined;

  for (int i = 0; i < 4; i++) {
    d[String("relay") + String(i + 1)] = relayState[i];
  }

  JsonArray arr = d["channels"].to<JsonArray>();
  for (int i = 0; i < 4; i++) {
    JsonObject c = arr.add<JsonObject>();
    c["id"] = i + 1;
    c["name"] = channels[i].name;
    c["room"] = channels[i].room;
    c["mode"] = channels[i].mode;
    c["state"] = relayState[i];
  }

  char out[1800];
  serializeJson(d, out, sizeof(out));
  mqttClient.publish(TOPIC_STATUS, out, true);
}

void publishTelemetry() {
  if (!mqttClient.connected()) return;

  JsonDocument d;
  d["device_id"] = DEVICE_ID;
  d["profile_version"] = profileVersion;
  for (int i = 0; i < 4; i++) {
    d[String("relay") + String(i + 1)] = relayState[i];
  }

  char out[600];
  serializeJson(d, out, sizeof(out));
  mqttClient.publish(TOPIC_TELEMETRY, out);
}

void publishRegister() {
  if (!mqttClient.connected()) return;

  JsonDocument d;
  d["device_id"] = DEVICE_ID;
  d["type"] = DEVICE_TYPE;
  d["name"] = deviceName;
  d["room"] = deviceRoom;
  d["firmware"] = "4CH-1.0.0";
  d["ip"] = WiFi.localIP().toString();
  d["profile_version"] = profileVersion;
  d["config_topic"] = TOPIC_CONFIG;
  d["control_topic"] = TOPIC_CONTROL;
  d["status_topic"] = TOPIC_STATUS;
  d["channel_count"] = 4;
  d["capabilities"][0] = "relay";
  d["capabilities"][1] = "manual";
  d["capabilities"][2] = "schedule";
  d["capabilities"][3] = "channel_rooms";

  char out[1200];
  serializeJson(d, out, sizeof(out));
  mqttClient.publish(TOPIC_REGISTER, out, false);
  mqttClient.publish(TOPIC_ANNOUNCE, out, false);
}

void publishConfigAck(const char* status) {
  if (!mqttClient.connected()) return;

  JsonDocument d;
  d["device_id"] = DEVICE_ID;
  d["version"] = profileVersion;
  d["status"] = status;

  char out[350];
  serializeJson(d, out, sizeof(out));
  mqttClient.publish(TOPIC_CONFIG_ACK, out, true);
}

// =====================================================
// CONFIG
// =====================================================
void handleConfig(String message) {
  Serial.printf("[CONFIG] Received profile: %s\n", message.c_str());

  JsonDocument d;
  DeserializationError error = deserializeJson(d, message);
  if (error) {
    Serial.printf("[CONFIG] JSON ERROR: %s\n", error.c_str());
    publishConfigAck("JSON_ERROR");
    return;
  }

  const char* id = d["device_id"] | "";
  if (strlen(id) && strcmp(id, DEVICE_ID) != 0) {
    Serial.printf("[CONFIG] Ignored device_id=%s\n", id);
    return;
  }

  // Reset MUST happen before version validation.
  if (d["reset_profile"] | false) {
    resetProfileToDefault();
    return;
  }

  int version = d["version"] | 0;
  Serial.printf("[CONFIG] incoming_version=%d current_version=%d\n", version, profileVersion);

  if (version < profileVersion) {
    Serial.println("[CONFIG] Ignored old version");
    publishConfigAck("IGNORED_OLD_VERSION");
    return;
  }

  if (d["name"].is<const char*>()) {
    deviceName = d["name"].as<String>();
  }
  if (d["room"].is<const char*>()) {
    deviceRoom = d["room"].as<String>();
  }

  JsonObject settings = d["settings"].as<JsonObject>();
  JsonArray arr = settings["channels"].as<JsonArray>();
  if (arr.isNull() || arr.size() < 4) {
    Serial.println("[CONFIG] INVALID_SETTINGS: need 4 channels");
    publishConfigAck("INVALID_SETTINGS");
    return;
  }

  for (int i = 0; i < 4; i++) {
    JsonObject c = arr[i];
    String defaultChannelName = String("Relay ") + String(i + 1);
    channels[i].name = c["name"] | defaultChannelName;
    channels[i].room = c["room"] | "";
    channels[i].mode = c["mode"] | "manual";
    channels[i].defaultState = c["default_state"] | false;
    channels[i].scheduleOn = c["schedule_on"] | "08:00";
    channels[i].scheduleOff = c["schedule_off"] | "22:00";
    if (c["schedule_days"].is<JsonArray>()) {
      channels[i].scheduleDayMask = 0;
      JsonArray days = c["schedule_days"].as<JsonArray>();
      const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
      for (JsonVariant day : days) for (int d = 0; d < 7; d++) if (day == names[d]) channels[i].scheduleDayMask |= (1u << d);
    } else channels[i].scheduleDayMask = 0x7F;

    if (channels[i].mode != "manual" && channels[i].mode != "schedule") {
      channels[i].mode = "manual";
    }
  }

  profileVersion = version;
  saveProfile();
  applyInitialProfile();

  publishConfigAck("APPLIED");
  publishStatus();
  publishRegister();
  Serial.println("[CONFIG] Relay profile applied successfully");
}

// =====================================================
// CONTROL
// =====================================================
void handleRelayCommand(String message) {
  JsonDocument d;
  DeserializationError error = deserializeJson(d, message);

  if (!error) {
    const char* action = d["action"] | "";

    if (strcmp(action, "SET_CHANNEL") == 0) {
      int channel = d["channel"] | 0;
      bool state = d["state"] | false;
      setRelayState(channel, state);
      publishStatus();
      publishTelemetry();
      return;
    }

    if (strcmp(action, "SET_ALL") == 0) {
      bool state = d["state"] | false;
      setAllRelayState(state);
      publishStatus();
      publishTelemetry();
      return;
    }

    // Optional simple action compatibility.
    if (strcmp(action, "RELAY1_ON") == 0) { setRelayState(1, true); }
    else if (strcmp(action, "RELAY1_OFF") == 0) { setRelayState(1, false); }
    else if (strcmp(action, "RELAY2_ON") == 0) { setRelayState(2, true); }
    else if (strcmp(action, "RELAY2_OFF") == 0) { setRelayState(2, false); }
    else if (strcmp(action, "RELAY3_ON") == 0) { setRelayState(3, true); }
    else if (strcmp(action, "RELAY3_OFF") == 0) { setRelayState(3, false); }
    else if (strcmp(action, "RELAY4_ON") == 0) { setRelayState(4, true); }
    else if (strcmp(action, "RELAY4_OFF") == 0) { setRelayState(4, false); }
    else if (strcmp(action, "ALL_ON") == 0) { setAllRelayState(true); }
    else if (strcmp(action, "ALL_OFF") == 0) { setAllRelayState(false); }
    else return;

    publishStatus();
    publishTelemetry();
    return;
  }

  // Original plain-text command compatibility.
  if (message == "RELAY1_ON") setRelayState(1, true);
  else if (message == "RELAY1_OFF") setRelayState(1, false);
  else if (message == "RELAY2_ON") setRelayState(2, true);
  else if (message == "RELAY2_OFF") setRelayState(2, false);
  else if (message == "RELAY3_ON") setRelayState(3, true);
  else if (message == "RELAY3_OFF") setRelayState(3, false);
  else if (message == "RELAY4_ON") setRelayState(4, true);
  else if (message == "RELAY4_OFF") setRelayState(4, false);
  else if (message == "ALL_ON") setAllRelayState(true);
  else if (message == "ALL_OFF") setAllRelayState(false);
  else return;

  publishStatus();
  publishTelemetry();
}

// =====================================================
// SCHEDULE ENGINE
// =====================================================
void applySchedule() {
  if (quarantined) return;

  bool changed = false;
  for (int i = 0; i < 4; i++) {
    if (channels[i].mode != "schedule") continue;

    bool shouldOn = isWithinSchedule(channels[i].scheduleOn, channels[i].scheduleOff, channels[i].scheduleDayMask);
    if (relayState[i] != shouldOn) {
      Serial.printf("[SCENARIO] CH%d %s -> %s (%s-%s)\n",
        i + 1,
        channels[i].name.c_str(),
        shouldOn ? "ON" : "OFF",
        channels[i].scheduleOn.c_str(),
        channels[i].scheduleOff.c_str());
      setRelayState(i + 1, shouldOn, false);
      changed = true;
    }
  }

  if (changed) publishStatus();
}

// =====================================================
// SECURITY
// =====================================================
void handleSecurity(String message) {
  JsonDocument d;
  if (deserializeJson(d, message)) return;

  const char* action = d["action"] | "";

  if (strcmp(action, "QUARANTINE") == 0) {
    quarantined = true;
    securityState = "QUARANTINED";
    setAllRelayState(false);
  }
  else if (strcmp(action, "RELEASE") == 0) {
    quarantined = false;
    securityState = "NORMAL";
    applyInitialProfile();
    applySchedule();
  }
  else if (strcmp(action, "NORMAL") == 0) {
    quarantined = false;
    securityState = "NORMAL";
  }
  else if (strcmp(action, "SUSPICIOUS") == 0) {
    securityState = "SUSPICIOUS";
  }
  else if (strcmp(action, "ATTACKED") == 0) {
    securityState = "ATTACKED";
  }

  publishStatus();
}

// =====================================================
// MQTT CALLBACK
// =====================================================
void callback(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  String t = topic;
  Serial.printf("[MQTT RX] topic=%s payload=%s\n", t.c_str(), msg.c_str());

  if (t == TOPIC_CONTROL) {
    if (quarantined) {
      Serial.println("[CONTROL] Ignored because relay is quarantined");
      return;
    }
    handleRelayCommand(msg);
  }
  else if (t == TOPIC_SECURITY) {
    handleSecurity(msg);
  }
  else if (t == TOPIC_CONFIG) {
    handleConfig(msg);
  }
}

// =====================================================
// WIFI / MQTT
// =====================================================
bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry++ < 30) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] FAILED");
    return false;
  }

  Serial.printf("[WIFI] IP=%s Gateway=%s RSSI=%d\n",
    WiFi.localIP().toString().c_str(),
    WiFi.gatewayIP().toString().c_str(),
    WiFi.RSSI());
  return true;
}

bool testMQTTServer() {
  WiFiClient test;
  bool ok = test.connect(MQTT_SERVER, MQTT_PORT);
  test.stop();
  Serial.printf("[NETWORK] %s %s:%d\n", ok ? "TCP OK" : "TCP FAILED", MQTT_SERVER, MQTT_PORT);
  return ok;
}

bool connectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!testMQTTServer()) return false;

  String clientId = String(DEVICE_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);

  if (mqttClient.connect(clientId.c_str())) {
    bool a = mqttClient.subscribe(TOPIC_CONTROL, 1);
    bool b = mqttClient.subscribe(TOPIC_SECURITY, 1);
    bool c = mqttClient.subscribe(TOPIC_CONFIG, 1);

    Serial.printf("[MQTT] SUB control=%s security=%s config=%s topic=%s\n",
      a ? "OK" : "FAIL", b ? "OK" : "FAIL", c ? "OK" : "FAIL", TOPIC_CONFIG);

    publishRegister();
    publishStatus();
    publishTelemetry();
    Serial.println("[MQTT] CONNECTED");
    return true;
  }

  Serial.printf("[MQTT] Connection failed state=%d\n", mqttClient.state());
  return false;
}

// =====================================================
// SETUP
// =====================================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n=== ESP32 4-CHANNEL RELAY + MQTT ===");

  for (int i = 0; i < 4; i++) {
    pinMode(RELAY_PINS[i], OUTPUT);
    digitalWrite(RELAY_PINS[i], RELAY_OFF);
  }

  loadProfile();

  configTime(7 * 3600, 0, "pool.ntp.org", "time.nist.gov", "time.google.com");
  Serial.println("[TIME] NTP sync started (UTC+7)");

  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
  mqttClient.setCallback(callback);
  mqttClient.setBufferSize(2048);
  Serial.println("[MQTT] Buffer size=2048");

  connectWiFi();
  if (WiFi.status() == WL_CONNECTED) connectMQTT();

  applyInitialProfile();
}

// =====================================================
// LOOP
// =====================================================
void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();

  if (WiFi.status() == WL_CONNECTED && !mqttClient.connected()) {
    unsigned long now = millis();
    if (now - lastMqttReconnect >= MQTT_RECONNECT_INTERVAL) {
      lastMqttReconnect = now;
      connectMQTT();
    }
  }
  else if (mqttClient.connected()) {
    mqttClient.loop();

    if (millis() - lastTelemetry >= TELEMETRY_INTERVAL) {
      lastTelemetry = millis();
      publishStatus();
      publishTelemetry();
    }

    if (millis() - lastAnnounce >= ANNOUNCE_INTERVAL) {
      lastAnnounce = millis();
      publishRegister();
    }
  }

  if (millis() - lastScenarioCheck >= SCENARIO_INTERVAL) {
    lastScenarioCheck = millis();
    applySchedule();
  }

  delay(1);
}
