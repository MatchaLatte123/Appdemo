# SmartHome MQTT Demo – Device Profiles / Discovery

Bản cập nhật này bổ sung:

- ESP32 Fan lưu profile local bằng NVS/Preferences.
- ESP32 Fan tự đăng ký lên MQTT qua `smarthome/device/register` và `smarthome/device/announce`.
- AppDemo tự phát hiện thiết bị mới.
- AppDemo lưu profile local vào `device_profiles.json`.
- AppDemo cho phép đổi tên, phòng và setting quạt.
- AppDemo đồng bộ profile xuống ESP32 qua MQTT retained message.
- ESP32 xác nhận bằng topic `smarthome/device/fan01/config_ack`.
- Có version để tránh ghi đè cấu hình cũ.

## MQTT topics mới

- `smarthome/device/register`
- `smarthome/device/announce`
- `smarthome/device/fan01/config`
- `smarthome/device/fan01/config_ack`

## Fan profile

```json
{
  "device_id": "ESP32-FAN-01",
  "version": 1,
  "name": "Quạt phòng khách",
  "room": "Phòng khách",
  "settings": {
    "default_level": 2,
    "auto_mode": true,
    "temperature_on": 30,
    "temperature_off": 27
  }
}
```

## Cài AppDemo

```powershell
python -m pip install -r requirements.txt
python mqtt_test_console_v2.py
```

App sẽ tạo `device_profiles.json` cạnh file Python. Đây là local cache/profile của AppDemo.

## Cài PlatformIO cho Fan

`platformio.ini`:

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
monitor_speed = 115200
upload_speed = 115200
lib_deps =
    knolleary/PubSubClient
    adafruit/DHT sensor library
    adafruit/Adafruit Unified Sensor
    bblanchon/ArduinoJson
```

Đặt `fan_mqtt_v2.ino` thành `src/main.cpp`.

## Đồng bộ hoạt động như thế nào

1. ESP32 khởi động và đọc NVS.
2. ESP32 kết nối MQTT và publish register/announce.
3. App nhận announce.
4. Nếu chưa có profile, thiết bị xuất hiện trong `Discovered devices`.
5. Chọn thiết bị → `ADD SELECTED DEVICE`.
6. App lưu profile local và publish config với retain=true.
7. ESP32 nhận config, lưu NVS, áp dụng default level và gửi `config_ack`.
8. Lần sau ESP32 khởi động, NVS khôi phục profile và MQTT retained config có thể đồng bộ lại.

## Lưu ý về Backend

Bản AppDemo này chưa tạo SQL/FastAPI backend mới. MQTT broker retained config đóng vai trò kênh lưu/đồng bộ trung gian; `device_profiles.json` là local cache của AppDemo. Khi tích hợp FastAPI + SQL Server, có thể dùng cùng payload/version/topic làm giao thức đồng bộ.
