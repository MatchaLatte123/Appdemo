# Cập nhật kịch bản tự động cho ESP32-FAN-01

Bản cập nhật này được tích hợp trực tiếp vào AppDemo và firmware quạt của bộ `SmartHome_MQTT_Profile_Update`.

## MQTT
- Broker: `192.168.1.130`
- Port: `1883`
- Config: `smarthome/device/fan01/config`
- Status: `smarthome/device/fan01/status`
- Control: `smarthome/device/fan01/control`

## Kịch bản quạt
AppDemo > Device Manager > chọn `ESP32-FAN-01` > `EDIT PROFILE`.

Có 4 chế độ:
- `manual`: điều khiển thủ công.
- `temperature`: bật/tắt theo nhiệt độ.
- `schedule`: bật/tắt theo giờ.
- `both`: đồng thời thỏa điều kiện giờ và nhiệt độ.

### Theo nhiệt độ
- Bật khi `temperature >= temperature_on`.
- Tắt khi `temperature <= temperature_off`.
- Dùng hysteresis để tránh bật/tắt liên tục.

### Theo giờ
- `schedule_on`: HH:MM.
- `schedule_off`: HH:MM.
- Có hỗ trợ khoảng qua đêm, ví dụ `22:00 -> 06:00`.
- ESP32 đồng bộ NTP theo UTC+7.

### Đồng bộ profile
AppDemo lưu profile cục bộ trong `device_profiles.json`, tăng `version` khi Sync và gửi JSON tới MQTT config topic. ESP32 nhận profile, lưu vào NVS và trả `config_ack`.

## Firmware
Mở project `platformio_fan` bằng PlatformIO và upload `src/main.cpp`.

GPIO:
- DHT11 DATA: GPIO4
- L298N ENA: GPIO25
- L298N IN1: GPIO26
- L298N IN2: GPIO27

Không thay đổi MQTT server: `192.168.1.130:1883`.
