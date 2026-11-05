#include <Arduino.h>
#include <WiFi.h>

#define MQTT_MAX_PACKET_SIZE 1024

#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>
#include "mqtt_secrets.h"

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

const char* DEVICE_ID = "ESP32-FAN-01";
const char* DEVICE_TYPE = "fan";

// =====================================================
// MQTT TOPICS
// =====================================================

const char* TOPIC_REGISTER =
    "smarthome/device/register";

const char* TOPIC_ANNOUNCE =
    "smarthome/device/announce";

const char* TOPIC_STATUS =
    "smarthome/device/fan01/status";

const char* TOPIC_TELEMETRY =
    "smarthome/device/fan01/telemetry";

const char* TOPIC_CONTROL =
    "smarthome/device/fan01/control";

const char* TOPIC_SECURITY =
    "smarthome/security/fan01";

const char* TOPIC_CONFIG =
    "smarthome/device/fan01/config";

const char* TOPIC_CONFIG_ACK =
    "smarthome/device/fan01/config_ack";

// =====================================================
// DHT11
// =====================================================

#define DHT_PIN 4
#define DHT_TYPE DHT11

DHT dht(DHT_PIN, DHT_TYPE);

// =====================================================
// L298N
// =====================================================

#define ENA 25
#define IN1 26
#define IN2 27

#define PWM_CHANNEL 0
#define PWM_FREQ 5000
#define PWM_RESOLUTION 8

// =====================================================
// MQTT / NVS
// =====================================================

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
Preferences prefs;

// =====================================================
// DEVICE STATE
// =====================================================

int currentLevel = 0;
int currentPercent = 0;

String securityState = "NORMAL";
bool quarantined = false;

// =====================================================
// DEVICE PROFILE
// =====================================================

String deviceName = "Quạt phòng khách";
String roomName = "Phòng khách";

int defaultLevel = 0;

// manual
// temperature
// schedule
// both

String scenarioMode = "manual";

bool autoMode = false;

bool temperatureEnabled = false;

float tempOn = 30.0;
float tempOff = 27.0;

bool scheduleEnabled = false;

String scheduleOn = "08:00";
String scheduleOff = "22:00";
uint8_t scheduleDayMask = 0x7F; // Monday through Sunday

// Motion input is deliberately independent of scenarioMode.
bool motionEnabled = false;
String motionSensorId = "PIR-FAN-01";
int motionGpio = 22;
uint16_t motionHoldSeconds = 120;
uint8_t motionCooldownSeconds = 3;
bool motionRequest = false;
// Explicit user ON while in Manual mode. The configured default level alone
// must not start the fan when Motion is enabled.
bool manualRequest = false;
unsigned long lastMotionAt = 0;
unsigned long lastMotionEdgeAt = 0;
unsigned long lastPirEdgeAt = 0;
unsigned long pirHighStartedAt = 0;
bool pirLastState = LOW;
bool pirRawState = LOW;
bool pirReady = false;
bool pirArmed = false;
volatile bool pirRisingPending = false;

bool wifiConnectPending = false;
bool wifiAttempted = false;
bool wifiWasConnected = false;
unsigned long wifiConnectStartedAt = 0;
unsigned long lastWiFiAttempt = 0;

int profileVersion = 0;

// =====================================================
// SCENARIO STATE
// =====================================================

bool autoFanState = false;

unsigned long lastMqttReconnect = 0;
unsigned long lastTelemetry = 0;
unsigned long lastAnnounce = 0;
unsigned long lastScenarioCheck = 0;

const unsigned long MQTT_RECONNECT_INTERVAL = 5000;
const unsigned long WIFI_CONNECT_TIMEOUT = 15000;
const unsigned long WIFI_RETRY_INTERVAL = 5000;
const unsigned long TELEMETRY_INTERVAL = 10000;
const unsigned long ANNOUNCE_INTERVAL = 30000;
const unsigned long SCENARIO_INTERVAL = 5000;

// =====================================================
// FORWARD DECLARATION
// =====================================================

void publishStatus();
void applyScenario();

void ARDUINO_ISR_ATTR pirRisingISR() {
  pirRisingPending = true;
}

bool isMotionActive() {
  return motionEnabled && motionRequest && !quarantined;
}

void initMotionSensor() {
  // GPIO22 is the board-wired SR602 input. Do not remap it from profile data.
  detachInterrupt(digitalPinToInterrupt(22));
  motionRequest = false;
  lastMotionAt = 0;
  lastMotionEdgeAt = 0;
  lastPirEdgeAt = 0;
  motionGpio = 22;
  if (motionEnabled) {
    pinMode(motionGpio, INPUT);
    pirRawState = digitalRead(motionGpio);
    pirLastState = pirRawState;
    pirHighStartedAt = pirRawState ? millis() : 0;
    // SR602 can power up with OUT high. Arm only after observing idle LOW.
    pirArmed = !pirRawState;
    noInterrupts();
    pirRisingPending = false;
    interrupts();
    attachInterrupt(digitalPinToInterrupt(motionGpio), pirRisingISR, RISING);
    pirReady = true;
  } else {
    pirReady = false;
    pirArmed = false;
    pirHighStartedAt = 0;
    motionRequest = false;
    noInterrupts();
    pirRisingPending = false;
    interrupts();
  }
}

bool processMotionSensor() {
  if (!motionEnabled || motionGpio != 22) {
    if (motionRequest) {
      motionRequest = false;
      return true;
    }
    return false;
  }
  if (!pirReady) initMotionSensor();

  const unsigned long now = millis();
  const bool pirState = digitalRead(motionGpio);
  bool risingPending;
  noInterrupts();
  risingPending = pirRisingPending;
  pirRisingPending = false;
  interrupts();
  pirRawState = pirState;

  if (quarantined) {
    bool changed = motionRequest;
    motionRequest = false;
    pirLastState = pirState;
    pirArmed = !pirState;
    pirHighStartedAt = pirState ? now : 0;
    return changed;
  }

  if (!pirArmed) {
    if (risingPending) {
      // An interrupt confirms LOW->HIGH even if the main loop missed the LOW.
      pirArmed = true;
    } else {
      pirLastState = pirState;
      if (!pirState) {
        pirArmed = true;
        pirHighStartedAt = 0;
        Serial.println("[MOTION] PIR armed after idle LOW");
      }
      return false;
    }
  }

  bool changed = false;
  const bool rawRising = pirState == HIGH && pirLastState == LOW;
  const bool risingEdge = risingPending || rawRising;
  if (risingEdge) {
    lastPirEdgeAt = now;
  }
  const bool acceptedRising = risingEdge &&
      (!lastMotionEdgeAt || now - lastMotionEdgeAt >= (unsigned long)motionCooldownSeconds * 1000UL);
  if (acceptedRising) {
    lastMotionEdgeAt = now;
    Serial.printf("[MOTION] %s detected, hold=%u s\n", motionSensorId.c_str(), motionHoldSeconds);
  }

  // Keep Motion active for the entire sensor HIGH level, then start the
  // configured hold timer after OUT returns LOW. The ISR preserves short
  // rising pulses while Wi-Fi/MQTT code is busy.
  if (pirState == HIGH || acceptedRising) {
    if (!motionRequest) changed = true;
    motionRequest = true;
    if (pirState == HIGH || acceptedRising) lastMotionAt = now;
    if (rawRising || (risingPending && pirState == HIGH) || !pirHighStartedAt) {
      pirHighStartedAt = now;
    }
  } else if (motionRequest && now - lastMotionAt >= (unsigned long)motionHoldSeconds * 1000UL) {
    motionRequest = false;
    changed = true;
    Serial.println("[MOTION] hold expired");
  }
  if (pirState == LOW) pirHighStartedAt = 0;
  pirLastState = pirState;
  return changed;
}

void appendMotionStatus(JsonDocument &d) {
  d["motion_enabled"] = motionEnabled;
  d["motion_sensor_id"] = motionSensorId;
  d["motion_available"] = motionEnabled && motionGpio == 22 && pirReady;
  d["motion_active"] = isMotionActive();
  unsigned long remaining = 0;
  if (motionRequest && motionHoldSeconds > 0) {
    unsigned long elapsed = (millis() - lastMotionAt) / 1000UL;
    if (elapsed < motionHoldSeconds) remaining = motionHoldSeconds - elapsed;
  }
  d["motion_remaining_seconds"] = remaining;
  d["pir_gpio_high"] = pirRawState;
  d["pir_armed"] = pirArmed;
  d["pir_last_rising_age_seconds"] = lastPirEdgeAt
      ? (millis() - lastPirEdgeAt) / 1000UL
      : 0;
  d["pir_high_seconds"] = pirRawState && pirHighStartedAt
      ? (millis() - pirHighStartedAt) / 1000UL
      : 0;
}

// =====================================================
// SAVE PROFILE
// =====================================================

void saveProfile() {

  if (!prefs.begin("device", false)) {
    Serial.println("[PROFILE] NVS open failed");
    return;
  }

  prefs.putString("name", deviceName);
  prefs.putString("room", roomName);

  prefs.putInt(
      "deflvl",
      defaultLevel
  );

  prefs.putString(
      "mode",
      scenarioMode
  );

  prefs.putBool(
      "auto",
      autoMode
  );

  prefs.putBool(
      "ten",
      temperatureEnabled
  );

  prefs.putFloat(
      "ton",
      tempOn
  );

  prefs.putFloat(
      "toff",
      tempOff
  );

  prefs.putBool(
      "sen",
      scheduleEnabled
  );

  prefs.putString(
      "son",
      scheduleOn
  );

  prefs.putString(
      "soff",
      scheduleOff
  );
  prefs.putUChar("sdays", scheduleDayMask);
  prefs.putBool("men", motionEnabled);
  prefs.putString("msid", motionSensorId);
  prefs.putUChar("mgpio", (uint8_t)motionGpio);
  prefs.putUShort("mhold", motionHoldSeconds);
  prefs.putUChar("mcool", motionCooldownSeconds);

  prefs.putInt(
      "ver",
      profileVersion
  );

  prefs.end();

  Serial.println("[PROFILE] Saved to NVS");
}

// =====================================================
// LOAD PROFILE
// =====================================================

void loadProfile() {

  if (!prefs.begin("device", false)) {

    Serial.println(
        "[PROFILE] NVS unavailable -> defaults"
    );

    return;
  }

  deviceName =
      prefs.getString(
          "name",
          "Quạt phòng khách"
      );

  roomName =
      prefs.getString(
          "room",
          "Phòng khách"
      );

  defaultLevel =
      constrain(
          prefs.getInt(
              "deflvl",
              0
          ),
          0,
          3
      );

  scenarioMode =
      prefs.getString(
          "mode",
          "manual"
      );

  autoMode =
      prefs.getBool(
          "auto",
          false
      );

  temperatureEnabled =
      prefs.getBool(
          "ten",
          autoMode
      );

  tempOn =
      prefs.getFloat(
          "ton",
          30.0
      );

  tempOff =
      prefs.getFloat(
          "toff",
          27.0
      );

  scheduleEnabled =
      prefs.getBool(
          "sen",
          false
      );

  scheduleOn =
      prefs.getString(
          "son",
          "08:00"
      );

  scheduleOff =
      prefs.getString(
          "soff",
          "22:00"
      );
  scheduleDayMask = prefs.getUChar("sdays", 0x7F);
  motionEnabled = prefs.getBool("men", false);
  motionSensorId = prefs.getString("msid", "PIR-FAN-01");
  motionGpio = prefs.getUChar("mgpio", 22);
  motionHoldSeconds = constrain(prefs.getUShort("mhold", 120), 1, 3600);
  motionCooldownSeconds = constrain(prefs.getUChar("mcool", 3), 0, 60);
  if (motionGpio != 22) {
    Serial.printf("[MOTION] Unsupported GPIO %d; disabling Motion (hardware pin is GPIO22)\n", motionGpio);
    motionEnabled = false;
    motionGpio = 22;
  }

  profileVersion =
      prefs.getInt(
          "ver",
          0
      );

  prefs.end();

  if (
      scenarioMode != "manual" &&
      scenarioMode != "temperature" &&
      scenarioMode != "schedule" &&
      scenarioMode != "both"
  ) {

    scenarioMode = "manual";
  }

  Serial.printf(
      "[PROFILE] %s | room=%s | mode=%s | level=%d | temp=%s %.1f/%.1f | schedule=%s %s-%s | ver=%d\n",
      deviceName.c_str(),
      roomName.c_str(),
      scenarioMode.c_str(),
      defaultLevel,
      temperatureEnabled ? "ON" : "OFF",
      tempOn,
      tempOff,
      scheduleEnabled ? "ON" : "OFF",
      scheduleOn.c_str(),
      scheduleOff.c_str(),
      profileVersion
  );
}

// =====================================================
// SET FAN LEVEL
// =====================================================

void setFanLevel(int level) {

  level = constrain(
      level,
      0,
      3
  );

  if (quarantined) {
    level = 0;
  }

  // ---------------------------------------------------
  // OFF
  // ---------------------------------------------------

  if (level == 0) {

    ledcWrite(
        PWM_CHANNEL,
        0
    );

    digitalWrite(
        IN1,
        LOW
    );

    digitalWrite(
        IN2,
        LOW
    );

    currentLevel = 0;
    currentPercent = 0;

    Serial.println(
        "[FAN] OFF"
    );

    return;
  }

  // ---------------------------------------------------
  // DIRECTION
  // ---------------------------------------------------

  digitalWrite(
      IN1,
      HIGH
  );

  digitalWrite(
      IN2,
      LOW
  );

  // ---------------------------------------------------
  // SPEED
  // ---------------------------------------------------

  int pwm =
      (level == 1)
          ? 204
          : (level == 2 ? 230 : 255);

  int percent =
      (level == 1)
          ? 80
          : (level == 2 ? 90 : 100);

  // Kick-start motor
  if (currentLevel == 0) {

    ledcWrite(
        PWM_CHANNEL,
        255
    );

    delay(500);
  }

  ledcWrite(
      PWM_CHANNEL,
      pwm
  );

  currentLevel = level;
  currentPercent = percent;

  Serial.printf(
      "[FAN] LEVEL %d - %d%%\n",
      level,
      percent
  );
}

// =====================================================
// READ TEMPERATURE
// =====================================================

float readTemperature() {

  float v =
      dht.readTemperature();

  return isnan(v)
      ? NAN
      : v;
}

// =====================================================
// READ HUMIDITY
// =====================================================

float readHumidity() {

  float v =
      dht.readHumidity();

  return isnan(v)
      ? NAN
      : v;
}

// =====================================================
// PARSE HH:MM
// =====================================================

bool parseHHMM(
    const String& value,
    int& minutes
) {

  int colon =
      value.indexOf(':');

  if (colon < 1) {
    return false;
  }

  int h =
      value.substring(
          0,
          colon
      ).toInt();

  int m =
      value.substring(
          colon + 1
      ).toInt();

  if (
      h < 0 ||
      h > 23 ||
      m < 0 ||
      m > 59
  ) {

    return false;
  }

  minutes =
      h * 60 + m;

  return true;
}

// =====================================================
// GET LOCAL CLOCK
// =====================================================

bool getLocalClock(
    struct tm& timeinfo
) {

  if (
      !getLocalTime(
          &timeinfo,
          1000
      )
  ) {

    Serial.println(
        "[SCENARIO] NTP time not ready"
    );

    return false;
  }

  if (
      timeinfo.tm_year <
      (2020 - 1900)
  ) {

    Serial.println(
        "[SCENARIO] Invalid system time"
    );

    return false;
  }

  return true;
}

// =====================================================
// CHECK SCHEDULE
// =====================================================

bool isWithinSchedule() {

  struct tm timeinfo;

  if (
      !getLocalClock(
          timeinfo
      )
  ) {

    return false;
  }

  int nowMin =
      timeinfo.tm_hour * 60 +
      timeinfo.tm_min;

  int onMin;
  int offMin;

  if (
      !parseHHMM(
          scheduleOn,
          onMin
      ) ||
      !parseHHMM(
          scheduleOff,
          offMin
      )
  ) {

    Serial.println(
        "[SCENARIO] Invalid schedule"
    );

    return false;
  }

  // For overnight windows, the selected day is the day the window starts.
  int dayBit = (timeinfo.tm_wday + 6) % 7; // Monday=bit 0
  if (onMin > offMin && nowMin < offMin) dayBit = (dayBit + 6) % 7;
  if (!(scheduleDayMask & (1u << dayBit))) return false;

  // Same time = always active
  if (onMin == offMin) {
    return true;
  }

  // Normal schedule
  if (onMin < offMin) {

    return
        nowMin >= onMin &&
        nowMin < offMin;
  }

  // Overnight schedule
  // Example: 22:00 -> 06:00

  return
      nowMin >= onMin ||
      nowMin < offMin;
}

// =====================================================
// APPLY SCENARIO
// =====================================================

void applyScenario() {

  if (quarantined) {
    return;
  }

  bool shouldRun = scenarioMode == "manual" &&
      (motionEnabled ? manualRequest : defaultLevel > 0);

  bool timeOK = true;
  bool tempOK = true;

  float temperature =
      readTemperature();

  // ---------------------------------------------------
  // TEMPERATURE
  // ---------------------------------------------------

  if (
      scenarioMode == "temperature" ||
      scenarioMode == "both"
  ) {

    if (isnan(temperature)) {
      Serial.println("[SCENARIO] Temperature unavailable; preserving hysteresis state");
    }

    // Hysteresis
    //
    // ON  >= tempOn
    // OFF <= tempOff

    if (!isnan(temperature) &&
        !autoFanState &&
        temperature >= tempOn
    ) {

      autoFanState = true;

    }
    else if (!isnan(temperature) &&
        autoFanState &&
        temperature <= tempOff
    ) {

      autoFanState = false;
    }

    tempOK =
        autoFanState;
  }

  // ---------------------------------------------------
  // SCHEDULE
  // ---------------------------------------------------

  if (
      scenarioMode == "schedule" ||
      scenarioMode == "both"
  ) {

    timeOK =
        isWithinSchedule();
  }

  // ---------------------------------------------------
  // DETERMINE TARGET STATE
  // ---------------------------------------------------

  if (
      scenarioMode == "temperature"
  ) {

    shouldRun = tempOK;

  }
  else if (
      scenarioMode == "schedule"
  ) {

    shouldRun =
        timeOK;

  }
  else if (
      scenarioMode == "both"
  ) {

    shouldRun =
        timeOK &&
        tempOK;
  }

  // In automatic modes, enabled Motion gates the scenario request. In Manual,
  // Motion remains an additional request and manual level is retained for restore.
  if (motionEnabled && scenarioMode != "manual") {
    shouldRun = shouldRun && isMotionActive();
  } else {
    shouldRun = shouldRun || isMotionActive();
  }

  // ---------------------------------------------------
  // TARGET LEVEL
  // ---------------------------------------------------

  int target =
      shouldRun
          ? defaultLevel
          : 0;

  target =
      constrain(
          target,
          0,
          3
      );

  // ---------------------------------------------------
  // APPLY
  // ---------------------------------------------------

  if (
      currentLevel != target
  ) {

    Serial.printf(
        "[SCENARIO] mode=%s temp=%.1f time=%s tempState=%s -> level=%d\n",
        scenarioMode.c_str(),
        temperature,
        timeOK ? "ON" : "OFF",
        tempOK ? "ON" : "OFF",
        target
    );

    setFanLevel(
        target
    );

    publishStatus();
  }

  // ---------------------------------------------------
  // DEBUG
  // ---------------------------------------------------

  static unsigned long lastScenarioDebug = 0;

  if (
      millis() - lastScenarioDebug >= 10000
  ) {

    lastScenarioDebug = millis();

    Serial.printf(
        "[SCENARIO CHECK] mode=%s temp=%.1f thresholds=%.1f/%.1f tempState=%s timeOK=%s default=%d target=%d\n",
        scenarioMode.c_str(),
        temperature,
        tempOn,
        tempOff,
        tempOK ? "ON" : "OFF",
        timeOK ? "ON" : "OFF",
        defaultLevel,
        target
    );
  }
}

// =====================================================
// PUBLISH STATUS
// =====================================================

void publishStatus() {

  if (!mqttClient.connected()) {
    return;
  }

  JsonDocument d;

  d["device_id"] =
      DEVICE_ID;

  d["device_type"] =
      DEVICE_TYPE;

  d["name"] =
      deviceName;

  d["room"] =
      roomName;

  d["online"] =
      true;

  float t =
      readTemperature();

  float h =
      readHumidity();

  if (!isnan(t)) {
    d["temperature"] =
        t;
  }

  if (!isnan(h)) {
    d["humidity"] =
        h;
  }

  d["fan_level"] =
      currentLevel;

  d["fan_percent"] =
      currentPercent;

  d["security_state"] =
      securityState;

  d["quarantined"] =
      quarantined;

  d["profile_version"] =
      profileVersion;

  d["scenario_mode"] =
      scenarioMode;

  appendMotionStatus(d);

  char out[900];

  serializeJson(
      d,
      out,
      sizeof(out)
  );

  mqttClient.publish(
      TOPIC_STATUS,
      out,
      true
  );
}

// =====================================================
// PUBLISH TELEMETRY
// =====================================================

void publishTelemetry() {

  if (!mqttClient.connected()) {
    return;
  }

  JsonDocument d;

  d["device_id"] =
      DEVICE_ID;

  float t =
      readTemperature();

  float h =
      readHumidity();

  if (!isnan(t)) {
    d["temperature"] =
        t;
  }

  if (!isnan(h)) {
    d["humidity"] =
        h;
  }

  d["fan_level"] =
      currentLevel;

  d["fan_percent"] =
      currentPercent;

  appendMotionStatus(d);

  char out[550];

  serializeJson(
      d,
      out,
      sizeof(out)
  );

  mqttClient.publish(
      TOPIC_TELEMETRY,
      out
  );
}

// =====================================================
// PUBLISH REGISTER
// =====================================================

void publishRegister() {

  if (!mqttClient.connected()) {
    return;
  }

  JsonDocument d;

  d["device_id"] =
      DEVICE_ID;

  d["type"] =
      DEVICE_TYPE;

  d["name"] =
      deviceName;

  d["room"] =
      roomName;

  d["firmware"] =
      "2.2.0";

  d["ip"] =
      WiFi.localIP().toString();

  d["profile_version"] =
      profileVersion;

  d["config_topic"] =
      TOPIC_CONFIG;

  d["status_topic"] =
      TOPIC_STATUS;

  d["capabilities"][0] =
      "power";

  d["capabilities"][1] =
      "speed";

  d["capabilities"][2] =
      "temperature";

  d["capabilities"][3] =
      "humidity";

  d["capabilities"][4] =
      "temperature_scenario";

  d["capabilities"][5] =
      "time_scenario";

  char out[750];

  serializeJson(
      d,
      out,
      sizeof(out)
  );

  mqttClient.publish(
      TOPIC_REGISTER,
      out,
      false
  );

  // ANNOUNCE is discovery only and must NOT be retained.
  // Otherwise the broker keeps an old FAN announcement and
  // the App can still discover this device after it is powered off.
  mqttClient.publish(
      TOPIC_ANNOUNCE,
      out,
      false
  );
}

// =====================================================
// PUBLISH CONFIG ACK
// =====================================================

void publishConfigAck(
    const char* status
) {

  if (!mqttClient.connected()) {
    return;
  }

  JsonDocument d;

  d["device_id"] =
      DEVICE_ID;

  d["version"] =
      profileVersion;

  d["status"] =
      status;

  char out[300];

  serializeJson(
      d,
      out,
      sizeof(out)
  );

  mqttClient.publish(
      TOPIC_CONFIG_ACK,
      out,
      true
  );
}

// =====================================================
// RESET PROFILE TO DEFAULT
// =====================================================
//
// This reset is intentionally handled before the normal
// version comparison. A deleted device may have a high
// profileVersion in NVS, so a normal version=0 config would
// otherwise be rejected as an old profile.
//
// The existing runtime/scenario/MQTT/security functions are
// preserved; this only adds an explicit profile reset path.
// =====================================================

void resetProfileToDefault() {

  Serial.println("[PROFILE] RESET requested -> restoring default profile");

  // Default profile used when a device is removed and later added again.
  deviceName = "Quạt phòng khách";
  roomName = "Phòng khách";

  defaultLevel = 1;

  scenarioMode = "manual";
  autoMode = false;

  temperatureEnabled = false;
  tempOn = 30.0;
  tempOff = 27.0;

  scheduleEnabled = false;
  scheduleOn = "08:00";
  scheduleOff = "22:00";
  scheduleDayMask = 0x7F;
  motionEnabled = false;
  motionSensorId = "PIR-FAN-01";
  motionGpio = 22;
  motionHoldSeconds = 120;
  motionCooldownSeconds = 3;
  motionRequest = false;
  manualRequest = false;
  pirArmed = false;
  pirRawState = LOW;
  lastPirEdgeAt = 0;
  pirHighStartedAt = 0;
  lastMotionAt = 0;
  lastMotionEdgeAt = 0;
  initMotionSensor();

  // Reset profile version so the next Add + Sync starts at version 1.
  profileVersion = 0;

  // Reset scenario/security runtime state as well.
  autoFanState = false;
  quarantined = false;
  securityState = "NORMAL";

  saveProfile();

  // The default profile is manual level 1.
  setFanLevel(defaultLevel);

  publishConfigAck("RESET_DEFAULT");
  publishStatus();
  publishRegister();

  Serial.println("[PROFILE] Default profile restored");
}

// =====================================================
// HANDLE CONFIG
// =====================================================

void handleConfig(
    String message
) {

  const bool previousMotionEnabled = motionEnabled;
  const String previousScenarioMode = scenarioMode;

  Serial.printf(
      "[CONFIG] Received profile: %s\n",
      message.c_str()
  );

  JsonDocument d;

  DeserializationError error =
      deserializeJson(
          d,
          message
      );

  if (error) {

    Serial.printf(
        "[CONFIG] JSON ERROR: %s\n",
        error.c_str()
    );

    publishConfigAck(
        "JSON_ERROR"
    );

    return;
  }

  const char* id =
      d["device_id"] | "";

  if (
      strlen(id) &&
      strcmp(
          id,
          DEVICE_ID
      ) != 0
  ) {

    Serial.printf(
        "[CONFIG] Ignored device_id=%s\n",
        id
    );

    return;
  }

  // ---------------------------------------------------
  // RESET PROFILE
  // ---------------------------------------------------
  // This must be processed BEFORE the version check.
  // A deleted device may have a large old version in NVS.
  // The reset command is therefore allowed to restore
  // the default profile regardless of the old version.
  if (
      d["reset_profile"] |
      false
  ) {

    resetProfileToDefault();
    return;
  }

  int version =
      d["version"] | 0;

  Serial.printf(
      "[CONFIG] incoming_version=%d current_version=%d\n",
      version,
      profileVersion
  );

  if (
      version < profileVersion
  ) {

    Serial.println(
        "[CONFIG] Ignored old version"
    );

    publishConfigAck(
        "IGNORED_OLD_VERSION"
    );

    return;
  }

  // ---------------------------------------------------
  // NAME / ROOM
  // ---------------------------------------------------

  if (
      d["name"].is<const char*>()
  ) {

    deviceName =
        d["name"].as<String>();
  }

  if (
      d["room"].is<const char*>()
  ) {

    roomName =
        d["room"].as<String>();
  }

  // ---------------------------------------------------
  // SETTINGS
  // ---------------------------------------------------

  if (
      d["settings"]["default_level"].is<int>()
  ) {

    defaultLevel =
        constrain(
            d["settings"]["default_level"].as<int>(),
            0,
            3
        );
  }

  if (
      d["settings"]["scenario_mode"].is<const char*>()
  ) {

    scenarioMode =
        d["settings"]["scenario_mode"].as<String>();
  }

  if (
      d["settings"]["auto_mode"].is<bool>()
  ) {

    autoMode =
        d["settings"]["auto_mode"].as<bool>();
  }

  // ---------------------------------------------------
  // TEMPERATURE
  // ---------------------------------------------------

  if (
      !d["settings"]["temperature_enabled"].isNull()
  ) {

    temperatureEnabled =
        d["settings"]["temperature_enabled"].as<bool>();
  }

  if (
      !d["settings"]["temperature_on"].isNull()
  ) {

    tempOn =
        d["settings"]["temperature_on"].as<float>();
  }

  if (
      !d["settings"]["temperature_off"].isNull()
  ) {

    tempOff =
        d["settings"]["temperature_off"].as<float>();
  }

  // ---------------------------------------------------
  // SCHEDULE
  // ---------------------------------------------------

  if (
      !d["settings"]["schedule_enabled"].isNull()
  ) {

    scheduleEnabled =
        d["settings"]["schedule_enabled"].as<bool>();
  }

  if (
      d["settings"]["schedule_on"].is<const char*>()
  ) {

    scheduleOn =
        d["settings"]["schedule_on"].as<String>();
  }

  if (
      d["settings"]["schedule_off"].is<const char*>()
  ) {

    scheduleOff =
        d["settings"]["schedule_off"].as<String>();
  }

  if (d["settings"]["schedule_days"].is<JsonArray>()) {
    scheduleDayMask = 0;
    JsonArray days = d["settings"]["schedule_days"].as<JsonArray>();
    const char* names[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    for (JsonVariant day : days) {
      for (int i = 0; i < 7; i++) if (day == names[i]) scheduleDayMask |= (1u << i);
    }
  } else scheduleDayMask = 0x7F;

  JsonObject motion = d["settings"]["motion"];
  if (!motion.isNull()) {
    if (motion["enabled"].is<bool>()) motionEnabled = motion["enabled"].as<bool>();
    if (motion["sensor_id"].is<const char*>()) motionSensorId = motion["sensor_id"].as<String>();
    int requestedGpio = motion["gpio"] | 22;
    motionGpio = requestedGpio;
    motionHoldSeconds = constrain(motion["hold_seconds"] | 120, 1, 3600);
    motionCooldownSeconds = constrain(motion["cooldown_seconds"] | 3, 0, 60);
    if (motionGpio != 22) {
      Serial.printf("[CONFIG] Motion GPIO %d rejected; Fan PIR is wired to GPIO22\n", motionGpio);
      motionEnabled = false;
      motionGpio = 22;
    }
  }
  if (!motionEnabled) motionRequest = false;
  if (!previousMotionEnabled && motionEnabled) manualRequest = false;
  if (scenarioMode != previousScenarioMode) manualRequest = false;
  if (motionEnabled != previousMotionEnabled || !pirReady) {
    initMotionSensor();
  }

  // ---------------------------------------------------
  // VALIDATE SCENARIO MODE
  // ---------------------------------------------------

  if (
      scenarioMode != "manual" &&
      scenarioMode != "temperature" &&
      scenarioMode != "schedule" &&
      scenarioMode != "both"
  ) {

    Serial.println(
        "[CONFIG] Invalid scenario_mode -> manual"
    );

    scenarioMode =
        "manual";

  }

  // scenario_mode is authoritative

  temperatureEnabled =
      (
          scenarioMode == "temperature" ||
          scenarioMode == "both"
      );

  scheduleEnabled =
      (
          scenarioMode == "schedule" ||
          scenarioMode == "both"
      );

  // ---------------------------------------------------
  // VALIDATE DEFAULT LEVEL
  // ---------------------------------------------------

  if (
      scenarioMode != "manual" &&
      defaultLevel == 0
  ) {

    Serial.println(
        "[CONFIG] WARNING: scenario selected but default_level=0"
    );
  }

  // ---------------------------------------------------
  // SAVE
  // ---------------------------------------------------

  profileVersion =
      version;

  saveProfile();

  autoFanState =
      false;

  // ---------------------------------------------------
  // APPLY IMMEDIATELY
  // ---------------------------------------------------

  applyScenario();

  // ---------------------------------------------------
  // ACK
  // ---------------------------------------------------

  publishConfigAck(
      "APPLIED"
  );

  publishStatus();

  publishRegister();

  Serial.println(
      "[CONFIG] Profile applied successfully"
  );
}

// =====================================================
// HANDLE FAN COMMAND
// =====================================================

void handleFanCommand(
    String message
) {

  JsonDocument d;

  DeserializationError error =
      deserializeJson(
          d,
          message
      );

  if (error) {

    Serial.printf(
        "[CONTROL] JSON ERROR: %s\n",
        error.c_str()
    );

    return;
  }

  const char* action =
      d["action"] | "";

  if (quarantined) {

    Serial.println(
        "[CONTROL] Ignored because device is quarantined"
    );

    return;
  }

  // ---------------------------------------------------
  // SET LEVEL
  // ---------------------------------------------------

  if (
      strcmp(
          action,
          "SET_LEVEL"
      ) == 0
  ) {

    int level =
        d["level"] | 0;
    level = constrain(level, 0, 3);

    Serial.printf(
        "[CONTROL] Manual SET_LEVEL=%d -> scenarioMode=manual\n",
        level
    );

    scenarioMode =
        "manual";

    defaultLevel = level;
    manualRequest = level > 0;

    setFanLevel(
        level
    );

    publishStatus();
    publishTelemetry();

  }

  // ---------------------------------------------------
  // OFF
  // ---------------------------------------------------

  else if (
      strcmp(
          action,
          "OFF"
      ) == 0
  ) {

    Serial.println(
        "[CONTROL] Manual OFF -> scenarioMode=manual"
    );

    scenarioMode =
        "manual";

    defaultLevel = 0;
    manualRequest = false;

    setFanLevel(
        0
    );

    publishStatus();
    publishTelemetry();
  }
}

// =====================================================
// HANDLE SECURITY
// =====================================================

void handleSecurity(
    String message
) {

  JsonDocument d;

  DeserializationError error =
      deserializeJson(
          d,
          message
      );

  if (error) {

    Serial.printf(
        "[SECURITY] JSON ERROR: %s\n",
        error.c_str()
    );

    return;
  }

  const char* action =
      d["action"] | "";

  // ---------------------------------------------------
  // QUARANTINE
  // ---------------------------------------------------

  if (
      strcmp(
          action,
          "QUARANTINE"
      ) == 0
  ) {

    quarantined =
        true;

    motionRequest = false;
    pirArmed = false;
    noInterrupts();
    pirRisingPending = false;
    interrupts();

    securityState =
        "QUARANTINED";

    setFanLevel(
        0
    );
  }

  // ---------------------------------------------------
  // RELEASE
  // ---------------------------------------------------

  else if (
      strcmp(
          action,
          "RELEASE"
      ) == 0
  ) {

    quarantined =
        false;

    securityState =
        "NORMAL";

    applyScenario();
  }

  // ---------------------------------------------------
  // NORMAL
  // ---------------------------------------------------

  else if (
      strcmp(
          action,
          "NORMAL"
      ) == 0
  ) {

    securityState =
        "NORMAL";

    quarantined =
        false;
  }

  // ---------------------------------------------------
  // SUSPICIOUS
  // ---------------------------------------------------

  else if (
      strcmp(
          action,
          "SUSPICIOUS"
      ) == 0
  ) {

    securityState =
        "SUSPICIOUS";
  }

  // ---------------------------------------------------
  // ATTACKED
  // ---------------------------------------------------

  else if (
      strcmp(
          action,
          "ATTACKED"
      ) == 0
  ) {

    securityState =
        "ATTACKED";
  }

  publishStatus();
}

// =====================================================
// MQTT CALLBACK
// =====================================================

void callback(
    char* topic,
    byte* payload,
    unsigned int length
) {

  String msg;

  for (
      unsigned int i = 0;
      i < length;
      i++
  ) {

    msg +=
        (char)payload[i];
  }

  String t =
      topic;

  Serial.printf(
      "[MQTT RX] topic=%s payload=%s\n",
      t.c_str(),
      msg.c_str()
  );

  // ---------------------------------------------------
  // CONTROL
  // ---------------------------------------------------

  if (
      t == TOPIC_CONTROL
  ) {

    handleFanCommand(
        msg
    );
  }

  // ---------------------------------------------------
  // SECURITY
  // ---------------------------------------------------

  else if (
      t == TOPIC_SECURITY
  ) {

    handleSecurity(
        msg
    );
  }

  // ---------------------------------------------------
  // CONFIG
  // ---------------------------------------------------

  else if (
      t == TOPIC_CONFIG
  ) {

    handleConfig(
        msg
    );
  }
}

// =====================================================
// WIFI
// =====================================================

bool connectWiFi() {
  const unsigned long now = millis();

  if (WiFi.status() == WL_CONNECTED) {
    wifiConnectPending = false;
    if (!wifiWasConnected) {
      Serial.printf(
          "[WIFI] IP=%s Gateway=%s RSSI=%d\n",
          WiFi.localIP().toString().c_str(),
          WiFi.gatewayIP().toString().c_str(),
          WiFi.RSSI()
      );
    }
    wifiWasConnected = true;
    return true;
  }

  if (wifiWasConnected) {
    Serial.println("[WIFI] Connection lost");
    wifiWasConnected = false;
    wifiConnectPending = false;
  }

  if (wifiConnectPending) {
    if (now - wifiConnectStartedAt < WIFI_CONNECT_TIMEOUT) return false;
    Serial.println("[WIFI] Connection attempt timed out");
    wifiConnectPending = false;
  }

  if (wifiAttempted && now - lastWiFiAttempt < WIFI_RETRY_INTERVAL) {
    return false;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiAttempted = true;
  wifiConnectPending = true;
  wifiConnectStartedAt = now;
  lastWiFiAttempt = now;
  Serial.println("[WIFI] Connection attempt started");
  return false;
}

// =====================================================
// =====================================================
// CONNECT MQTT
// =====================================================

bool connectMQTT() {

  if (
      WiFi.status() != WL_CONNECTED
  ) {

    return false;
  }

  String clientId =
      String(
          DEVICE_ID
      ) +
      "-" +
      String(
          (uint32_t)ESP.getEfuseMac(),
          HEX
      );

  if (
    mqttClient.connect(
        clientId.c_str(),
        MQTT_USERNAME,
        MQTT_PASSWORD
    )
  ) {

    bool subControl =
        mqttClient.subscribe(
            TOPIC_CONTROL,
            1
        );

    bool subSecurity =
        mqttClient.subscribe(
            TOPIC_SECURITY,
            1
        );

    bool subConfig =
        mqttClient.subscribe(
            TOPIC_CONFIG,
            1
        );

    Serial.printf(
        "[MQTT] SUB control=%s security=%s config=%s topic=%s\n",
        subControl
            ? "OK"
            : "FAIL",
        subSecurity
            ? "OK"
            : "FAIL",
        subConfig
            ? "OK"
            : "FAIL",
        TOPIC_CONFIG
    );

    publishRegister();

    publishStatus();

    publishTelemetry();

    Serial.println(
        "[MQTT] CONNECTED"
    );

    return true;
  }

  Serial.printf(
      "[MQTT] Connection failed state=%d\n",
      mqttClient.state()
  );

  return false;
}

// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(
      115200
  );

  delay(1000);

  Serial.println(
      "\n=== ESP32 FAN + SCENARIOS ==="
  );

  // ---------------------------------------------------
  // LOAD PROFILE
  // ---------------------------------------------------

  loadProfile();
  initMotionSensor();

  // ---------------------------------------------------
  // L298N
  // ---------------------------------------------------

  pinMode(
      IN1,
      OUTPUT
  );

  pinMode(
      IN2,
      OUTPUT
  );

  digitalWrite(
      IN1,
      LOW
  );

  digitalWrite(
      IN2,
      LOW
  );

  ledcSetup(
      PWM_CHANNEL,
      PWM_FREQ,
      PWM_RESOLUTION
  );

  ledcAttachPin(
      ENA,
      PWM_CHANNEL
  );

  ledcWrite(
      PWM_CHANNEL,
      0
  );

  // ---------------------------------------------------
  // DHT
  // ---------------------------------------------------

  dht.begin();

  // ---------------------------------------------------
  // NTP
  // ---------------------------------------------------

  configTime(
      7 * 3600,
      0,
      "pool.ntp.org",
      "time.nist.gov",
      "time.google.com"
  );

  Serial.println(
      "[TIME] NTP sync started (UTC+7)"
  );

  // ---------------------------------------------------
  // MQTT
  // ---------------------------------------------------

  mqttClient.setServer(
      MQTT_SERVER,
      MQTT_PORT
  );

  // Bound the MQTT handshake response wait during reconnects.
  mqttClient.setSocketTimeout(2);

  mqttClient.setCallback(
      callback
  );

  // IMPORTANT:
  // Increase MQTT packet buffer so the
  // device profile JSON can be received.

  mqttClient.setBufferSize(
      1024
  );

  Serial.println(
      "[MQTT] Buffer size=1024"
  );

  // ---------------------------------------------------
  // CONNECT WIFI
  // ---------------------------------------------------

  connectWiFi();

  // ---------------------------------------------------
  // CONNECT MQTT
  // ---------------------------------------------------

  if (
      WiFi.status() == WL_CONNECTED
  ) {

    connectMQTT();
  }

  // ---------------------------------------------------
  // MANUAL INITIAL STATE
  // ---------------------------------------------------

  if (
      scenarioMode == "manual"
  ) {
    if (motionEnabled) {
      setFanLevel(manualRequest ? defaultLevel : 0);
    } else {
      setFanLevel(defaultLevel);
    }
  }
}

// =====================================================
// LOOP
// =====================================================

void loop() {

  if (processMotionSensor()) {
    applyScenario();
    publishStatus();
    publishTelemetry();
  }

  // ---------------------------------------------------
  // WIFI
  // ---------------------------------------------------

  connectWiFi();

  // ---------------------------------------------------
  // MQTT RECONNECT
  // ---------------------------------------------------

  if (
      WiFi.status() == WL_CONNECTED &&
      !mqttClient.connected()
  ) {

    unsigned long now =
        millis();

    if (
        now - lastMqttReconnect >=
        MQTT_RECONNECT_INTERVAL
    ) {

      lastMqttReconnect =
          now;

      connectMQTT();
    }
  }

  // ---------------------------------------------------
  // MQTT LOOP
  // ---------------------------------------------------

  else if (
      mqttClient.connected()
  ) {

    mqttClient.loop();

    // -------------------------------------------------
    // TELEMETRY
    // -------------------------------------------------

    if (
        millis() - lastTelemetry >=
        TELEMETRY_INTERVAL
    ) {

      lastTelemetry =
          millis();

      publishStatus();

      publishTelemetry();
    }

    // -------------------------------------------------
    // ANNOUNCE
    // -------------------------------------------------

    if (
        millis() - lastAnnounce >=
        ANNOUNCE_INTERVAL
    ) {

      lastAnnounce =
          millis();

      publishRegister();
    }
  }

  // ---------------------------------------------------
  // SCENARIO
  // ---------------------------------------------------

  if (
      millis() - lastScenarioCheck >=
      SCENARIO_INTERVAL
  ) {

    lastScenarioCheck =
        millis();

    applyScenario();
  }

  delay(1);
}
