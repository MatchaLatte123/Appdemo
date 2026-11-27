#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>
#include "mqtt_secrets.h"

// Forward declarations
void publishStatus();
void publishTelemetry();
void publishRegister();
void publishConfigAck(const char* status);
void setRelayState(int channel, bool state, bool publish = true);
void applySchedule();

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

// =====================================================
// PROFILE
// =====================================================
String deviceName = "ESP32-RELAY-01";
String deviceRoom = "";
int profileVersion = 0;

struct ChannelProfile {
  String name;
  String room;
  String automationMode; // manual | motion | schedule | schedule_motion
  String sensorId;
  int motionGpio;
  bool motionConfigured;
  uint16_t holdSeconds;
  uint16_t cooldownSeconds;
  bool defaultState;
  String scheduleOn;
  String scheduleOff;
  uint8_t scheduleDayMask;
};

ChannelProfile channels[4];
bool scheduleRequest[4] = {false, false, false, false};
bool motionRequest[4] = {false, false, false, false};
bool manualOverrideActive[4] = {false, false, false, false};
bool manualOverrideState[4] = {false, false, false, false};
unsigned long manualOverrideUntil[4] = {0, 0, 0, 0};
unsigned long lastMotionAt[4] = {0, 0, 0, 0};
unsigned long lastMotionEdgeAt[4] = {0, 0, 0, 0};
bool pirLastState[4] = {false, false, false, false};
bool pirReady[4] = {false, false, false, false};
const char* PIR_IDS[4] = {"PIR-SR602-01", "PIR-SR602-02", "PIR-SR602-03", "PIR-SR602-04"};
const int PIR_GPIOS[4] = {16, 17, 25, 26};
int pirActiveGpio[4] = {-1, -1, -1, -1};

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
  channels[index].automationMode = "manual";
  channels[index].sensorId = PIR_IDS[index];
  channels[index].motionGpio = PIR_GPIOS[index];
  channels[index].motionConfigured = false;
  channels[index].holdSeconds = 120;
  channels[index].cooldownSeconds = 3;
  channels[index].defaultState = false;
  channels[index].scheduleOn = "08:00";
  channels[index].scheduleOff = "22:00";
  channels[index].scheduleDayMask = 0x7F;
}

int gpioForPir(const String& sensorId) {
  for (int i = 0; i < 4; ++i) if (sensorId == PIR_IDS[i]) return PIR_GPIOS[i];
  return -1;
}

bool pirConfigured(int index) {
  if (index < 0 || index >= 4 || !channels[index].motionConfigured) return false;
  int assignedGpio = gpioForPir(channels[index].sensorId);
  return assignedGpio >= 0 && channels[index].motionGpio == assignedGpio;
}

void configurePirInputs() {
  for (int i = 0; i < 4; ++i) {
    if (pirReady[i] && pirActiveGpio[i] >= 0) pinMode(pirActiveGpio[i], INPUT);
    pirReady[i] = false;
    pirActiveGpio[i] = -1;
    if ((channels[i].automationMode == "motion" || channels[i].automationMode == "schedule_motion") && pirConfigured(i)) {
      pinMode(channels[i].motionGpio, INPUT);
      pirActiveGpio[i] = channels[i].motionGpio;
      pirLastState[i] = digitalRead(channels[i].motionGpio) == HIGH;
      pirReady[i] = true;
    }
    motionRequest[i] = false;
    lastMotionAt[i] = 0;
    lastMotionEdgeAt[i] = 0;
  }
}

void applyTargetStates() {
  bool changed = false;
  unsigned long now = millis();
  for (int i = 0; i < 4; ++i) {
    bool target = relayState[i];
    if (channels[i].automationMode == "manual") continue;
    bool requested = false;
    if (channels[i].automationMode == "motion") requested = motionRequest[i];
    else if (channels[i].automationMode == "schedule") requested = scheduleRequest[i];
    // In schedule_motion, scheduleRequest gates PIR processing; it is not itself an ON request.
    else if (channels[i].automationMode == "schedule_motion") requested = motionRequest[i];
    if (manualOverrideActive[i] && (long)(now - manualOverrideUntil[i]) < 0) {
      target = manualOverrideState[i];
    } else {
      manualOverrideActive[i] = false;
      target = requested;
    }
    if (target != relayState[i]) { setRelayState(i + 1, target, false); changed = true; }
  }
  if (changed) publishStatus();
}

void applyManualCommand(int channel, bool state) {
  if (channel < 1 || channel > 4) return;
  int i = channel - 1;
  if (channels[i].automationMode == "manual") {
    setRelayState(channel, state);
  } else {
    manualOverrideActive[i] = true;
    manualOverrideState[i] = state;
    manualOverrideUntil[i] = millis() + 30000UL;
    setRelayState(channel, state);
  }
}

void processPirInputs() {
  unsigned long now = millis();
  for (int i = 0; i < 4; ++i) {
    bool motionMode = channels[i].automationMode == "motion" || channels[i].automationMode == "schedule_motion";
    bool motionAllowed = channels[i].automationMode == "motion" ||
      (channels[i].automationMode == "schedule_motion" && scheduleRequest[i]);
    if (!pirReady[i] || !motionMode || !motionAllowed) {
      if (channels[i].automationMode == "schedule_motion" && !scheduleRequest[i]) motionRequest[i] = false;
      if (pirReady[i]) pirLastState[i] = digitalRead(channels[i].motionGpio) == HIGH;
      continue;
    }
    bool current = digitalRead(channels[i].motionGpio) == HIGH;
    if (current && !pirLastState[i] && (!lastMotionEdgeAt[i] || now - lastMotionEdgeAt[i] >= (unsigned long)channels[i].cooldownSeconds * 1000UL)) {
      lastMotionEdgeAt[i] = now;
      lastMotionAt[i] = now;
      motionRequest[i] = true;
      Serial.printf("[MOTION] CH%d movement detected\n", i + 1);
    }
    pirLastState[i] = current;
    if (motionRequest[i] && now - lastMotionAt[i] >= (unsigned long)channels[i].holdSeconds * 1000UL) {
      motionRequest[i] = false;
      Serial.printf("[MOTION] CH%d hold expired\n", i + 1);
    }
  }
}

void setDefaultProfile() {
  deviceName = DEVICE_ID;
  deviceRoom = "";
  profileVersion = 0;
  for (int i = 0; i < 4; i++) {
    setDefaultChannel(i);
    relayState[i] = false;
  }
}

// =====================================================
// RELAY OUTPUT
// =====================================================
void setRelayState(int channel, bool state, bool publish) {
  if (channel < 1 || channel > 4) return;

  int index = channel - 1;
  digitalWrite(RELAY_PINS[index], state ? RELAY_ON : RELAY_OFF);
  relayState[index] = state;

  Serial.printf("[RELAY] CH%d -> %s\n", channel, state ? "ON" : "OFF");
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
    c["automation_mode"] = channels[i].automationMode;
    c["mode"] = (channels[i].automationMode == "schedule" || channels[i].automationMode == "schedule_motion") ? "schedule" : "manual";
    JsonObject motion = c["motion"].to<JsonObject>();
    motion["sensor_id"] = channels[i].sensorId;
    motion["gpio"] = channels[i].motionGpio;
    motion["configured"] = channels[i].motionConfigured;
    motion["hold_seconds"] = channels[i].holdSeconds;
    motion["cooldown_seconds"] = channels[i].cooldownSeconds;
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
      String legacyMode = c["mode"] | "manual";
      channels[i].automationMode = c["automation_mode"] | (legacyMode == "schedule" ? "schedule" : "manual");
      channels[i].defaultState = c["default_state"] | false;
      channels[i].scheduleOn = c["schedule_on"] | "08:00";
      channels[i].scheduleOff = c["schedule_off"] | "22:00";
      if (c["schedule_days"].is<JsonArray>()) {
        channels[i].scheduleDayMask = 0;
        JsonArray days = c["schedule_days"].as<JsonArray>();
        const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
        for (JsonVariant day : days) for (int d = 0; d < 7; d++) if (day == names[d]) channels[i].scheduleDayMask |= (1u << d);
      }
      JsonObject motion = c["motion"].as<JsonObject>();
      channels[i].sensorId = motion["sensor_id"] | channels[i].sensorId;
      channels[i].motionGpio = motion["gpio"] | channels[i].motionGpio;
      channels[i].motionConfigured = motion["configured"] | false;
      channels[i].holdSeconds = constrain((int)(motion["hold_seconds"] | 120), 1, 3600);
      channels[i].cooldownSeconds = constrain((int)(motion["cooldown_seconds"] | 3), 0, 60);
      if (channels[i].automationMode != "manual" && channels[i].automationMode != "motion" && channels[i].automationMode != "schedule" && channels[i].automationMode != "schedule_motion") channels[i].automationMode = "manual";
    }
  }

  Serial.printf("[PROFILE] %s | room=%s | ver=%d\n", deviceName.c_str(), deviceRoom.c_str(), profileVersion);
  for (int i = 0; i < 4; i++) {
    Serial.printf("  CH%d %s | room=%s | mode=%s | default=%s | %s-%s\n",
      i + 1,
      channels[i].name.c_str(),
      channels[i].room.c_str(),
      channels[i].automationMode.c_str(),
      channels[i].defaultState ? "ON" : "OFF",
      channels[i].scheduleOn.c_str(),
      channels[i].scheduleOff.c_str());
  }
}

void applyInitialProfile() {
  for (int i = 0; i < 4; i++) {
    if (channels[i].automationMode == "manual") {
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

  for (int i = 0; i < 4; i++) {
    d[String("relay") + String(i + 1)] = relayState[i];
  }

  JsonArray arr = d["channels"].to<JsonArray>();
  for (int i = 0; i < 4; i++) {
    JsonObject c = arr.add<JsonObject>();
    c["id"] = i + 1;
    c["name"] = channels[i].name;
    c["room"] = channels[i].room;
    c["automation_mode"] = channels[i].automationMode;
    c["state"] = relayState[i];
    JsonObject motion = c["motion"].to<JsonObject>();
    motion["sensor_id"] = channels[i].sensorId;
    motion["gpio"] = channels[i].motionGpio;
    motion["active"] = motionRequest[i];
    motion["available"] = pirConfigured(i);
    motion["remaining_seconds"] = motionRequest[i] && millis() - lastMotionAt[i] < (unsigned long)channels[i].holdSeconds * 1000UL
      ? (unsigned long)(channels[i].holdSeconds - (millis() - lastMotionAt[i]) / 1000UL) : 0;
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
  d["capabilities"][4] = "motion_automation";

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
    String legacyMode = c["mode"] | "manual";
    channels[i].automationMode = c["automation_mode"] | (legacyMode == "schedule" ? "schedule" : "manual");
    channels[i].defaultState = c["default_state"] | false;
    channels[i].scheduleOn = c["schedule_on"] | "08:00";
    channels[i].scheduleOff = c["schedule_off"] | "22:00";
    if (c["schedule_days"].is<JsonArray>()) {
      channels[i].scheduleDayMask = 0;
      JsonArray days = c["schedule_days"].as<JsonArray>();
      const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
      for (JsonVariant day : days) for (int d = 0; d < 7; d++) if (day == names[d]) channels[i].scheduleDayMask |= (1u << d);
    } else channels[i].scheduleDayMask = 0x7F;

    JsonObject motion = c["motion"].as<JsonObject>();
    channels[i].sensorId = motion["sensor_id"] | channels[i].sensorId;
    channels[i].motionGpio = motion["gpio"] | channels[i].motionGpio;
    channels[i].motionConfigured = motion["configured"] | false;
    channels[i].holdSeconds = constrain((int)(motion["hold_seconds"] | 120), 1, 3600);
    channels[i].cooldownSeconds = constrain((int)(motion["cooldown_seconds"] | 3), 0, 60);
    if (channels[i].automationMode != "manual" && channels[i].automationMode != "motion" && channels[i].automationMode != "schedule" && channels[i].automationMode != "schedule_motion") channels[i].automationMode = "manual";
  }

  profileVersion = version;
  saveProfile();
  configurePirInputs();
  applyInitialProfile();
  applySchedule();

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
      applyManualCommand(channel, state);
      publishStatus();
      publishTelemetry();
      return;
    }

    if (strcmp(action, "SET_ALL") == 0) {
      bool state = d["state"] | false;
      for (int i = 1; i <= 4; ++i) applyManualCommand(i, state);
      publishStatus();
      publishTelemetry();
      return;
    }

    // Optional simple action compatibility.
    if (strcmp(action, "RELAY1_ON") == 0) { applyManualCommand(1, true); }
    else if (strcmp(action, "RELAY1_OFF") == 0) { applyManualCommand(1, false); }
    else if (strcmp(action, "RELAY2_ON") == 0) { applyManualCommand(2, true); }
    else if (strcmp(action, "RELAY2_OFF") == 0) { applyManualCommand(2, false); }
    else if (strcmp(action, "RELAY3_ON") == 0) { applyManualCommand(3, true); }
    else if (strcmp(action, "RELAY3_OFF") == 0) { applyManualCommand(3, false); }
    else if (strcmp(action, "RELAY4_ON") == 0) { applyManualCommand(4, true); }
    else if (strcmp(action, "RELAY4_OFF") == 0) { applyManualCommand(4, false); }
    else if (strcmp(action, "ALL_ON") == 0) { for (int i=1;i<=4;i++) applyManualCommand(i, true); }
    else if (strcmp(action, "ALL_OFF") == 0) { for (int i=1;i<=4;i++) applyManualCommand(i, false); }
    else return;

    publishStatus();
    publishTelemetry();
    return;
  }

  // Original plain-text command compatibility.
  if (message == "RELAY1_ON") applyManualCommand(1, true);
  else if (message == "RELAY1_OFF") applyManualCommand(1, false);
  else if (message == "RELAY2_ON") applyManualCommand(2, true);
  else if (message == "RELAY2_OFF") applyManualCommand(2, false);
  else if (message == "RELAY3_ON") applyManualCommand(3, true);
  else if (message == "RELAY3_OFF") applyManualCommand(3, false);
  else if (message == "RELAY4_ON") applyManualCommand(4, true);
  else if (message == "RELAY4_OFF") applyManualCommand(4, false);
  else if (message == "ALL_ON") { for (int i=1;i<=4;i++) applyManualCommand(i, true); }
  else if (message == "ALL_OFF") { for (int i=1;i<=4;i++) applyManualCommand(i, false); }
  else return;

  publishStatus();
  publishTelemetry();
}

// =====================================================
// SCHEDULE ENGINE
// =====================================================
void applySchedule() {
  for (int i = 0; i < 4; i++) {
    scheduleRequest[i] = (channels[i].automationMode == "schedule" || channels[i].automationMode == "schedule_motion") &&
      isWithinSchedule(channels[i].scheduleOn, channels[i].scheduleOff, channels[i].scheduleDayMask);
    if (channels[i].automationMode == "schedule_motion" && !scheduleRequest[i]) motionRequest[i] = false;
  }
  applyTargetStates();
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
    handleRelayCommand(msg);
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

  if (mqttClient.connect(clientId.c_str(), MQTT_USERNAME, MQTT_PASSWORD)) {
    bool a = mqttClient.subscribe(TOPIC_CONTROL, 1);
    bool c = mqttClient.subscribe(TOPIC_CONFIG, 1);

    Serial.printf("[MQTT] SUB control=%s config=%s topic=%s\n",
      a ? "OK" : "FAIL", c ? "OK" : "FAIL", TOPIC_CONFIG);

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

  configurePirInputs();
  applyInitialProfile();
  applySchedule();
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

  processPirInputs();
  applyTargetStates();

  delay(1);
}
