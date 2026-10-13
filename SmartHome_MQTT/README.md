# SmartHome AppDemo + 4-Channel Relay

Bản này được tạo **trực tiếp từ SmartHome AppDemo Normal v6**. Các chức năng Fan/Camera, MQTT, Discovery, Edit/Sync, Delete/Reset, Quarantine/Release và Camera Stream được giữ nguyên. Phần mới tập trung vào **ESP32 4-channel Relay**.

## 1. Chức năng Relay mới

- Kết nối ESP32-RELAY-01 qua MQTT.
- 4 channel relay.
- Điều khiển từng channel ON/OFF.
- ALL ON / ALL OFF.
- Đặt tên riêng cho relay device.
- Đặt tên riêng cho CH1..CH4.
- Tạo danh sách phòng.
- Gán một phòng cho một hoặc nhiều channel.
- Mỗi channel có 2 chế độ:
  - `manual`: điều khiển bằng nút ON/OFF.
  - `schedule`: tự bật trong khoảng giờ đã đặt và tự tắt ngoài khoảng giờ.
- Hỗ trợ schedule qua NTP UTC+7.
- Hỗ trợ lịch qua đêm, ví dụ `22:00 -> 06:00`.
- Lưu profile vào NVS.
- Xóa thiết bị: App gửi reset profile trước, ESP32 đưa profile về mặc định rồi App mới xóa profile cục bộ.
- Quarantine: tắt cả 4 relay.
- Release: khôi phục theo profile.

## 2. Chân relay

Giữ nguyên CH1/CH2 từ `RelayWebTest.ino` hiện tại:

| Channel | GPIO | Trạng thái ON |
|---|---:|---|
| CH1 | GPIO21 | HIGH |
| CH2 | GPIO22 | HIGH |
| CH3 | GPIO23 | HIGH |
| CH4 | GPIO19 | HIGH |

Nếu board relay của bạn dùng GPIO khác cho CH3/CH4, sửa 2 define ở đầu firmware:

```cpp
#define RELAY3_PIN 23
#define RELAY4_PIN 19
```

`RELAY_ON`/`RELAY_OFF` cũng được giữ theo firmware RelayWebTest hiện tại:

```cpp
#define RELAY_ON HIGH
#define RELAY_OFF LOW
```

## 3. Firmware

Có hai bản giống nhau:

```text
Relay_4CH_MQTT.ino
```

dùng cho Arduino IDE, và:

```text
platformio_relay/src/main.cpp
```

dùng cho project PlatformIO.

### Thư viện

Cần:

- PubSubClient
- ArduinoJson 7

ESP32 Arduino Core.

### MQTT

```text
Broker: 192.168.1.130
Port:   1883
```

Topics:

```text
smarthome/device/register
smarthome/device/announce
smarthome/device/relay01/status
smarthome/device/relay01/telemetry
smarthome/device/relay01/control
smarthome/device/relay01/config
smarthome/device/relay01/config_ack
smarthome/security/relay01
```

## 4. Profile Relay

Profile được App gửi theo dạng:

```json
{
  "device_id": "ESP32-RELAY-01",
  "version": 1,
  "name": "Relay phòng khách",
  "room": "",
  "settings": {
    "rooms": ["Phòng khách", "Phòng ngủ"],
    "channels": [
      {
        "id": 1,
        "name": "Đèn trần",
        "room": "Phòng khách",
        "mode": "schedule",
        "default_state": false,
        "schedule_on": "18:00",
        "schedule_off": "23:00"
      }
    ]
  }
}
```

Có đủ 4 object channel trong profile thực tế.

## 5. Thêm Relay

1. Chạy Mosquitto.
2. Mở AppDemo.
3. `CONNECT` tới `192.168.1.130:1883`.
4. ESP32-RELAY-01 khởi động và publish `announce/register`.
5. Nhấn `THÊM THIẾT BỊ`.
6. Chọn `relay`.
7. Chọn ESP32-RELAY-01.
8. Đặt tên relay và thêm thiết bị.
9. App tạo profile mặc định 4 channel rồi đồng bộ.

Profile mặc định:

```text
CH1 = Relay 1 | manual | OFF
CH2 = Relay 2 | manual | OFF
CH3 = Relay 3 | manual | OFF
CH4 = Relay 4 | manual | OFF
```

## 6. Cấu hình tên channel và phòng

Trong card relay chọn:

```text
EDIT PROFILE
```

Có thể:

- Đổi tên Relay device.
- Tạo phòng bằng `TẠO PHÒNG`.
- Chọn phòng cho từng CH.
- Gán cùng một phòng cho nhiều CH.
- Đổi tên CH1..CH4.
- Chọn `manual` hoặc `schedule` cho từng CH.
- Đặt Schedule ON/OFF cho từng CH.
- Đặt trạng thái mặc định khi mode là manual.

Ví dụ:

```text
Phòng khách
  CH1 Đèn trần       schedule 18:00 -> 23:00
  CH2 Đèn bàn        manual

Phòng ngủ
  CH3 Quạt           schedule 22:00 -> 06:00
  CH4 Đèn ngủ        manual
```

## 7. Xóa thiết bị

Khi nhấn `XÓA THIẾT BỊ`:

```text
App
 ↓
reset_profile=true
 ↓
ESP32-RELAY-01
 ↓
profile mặc định + version=0 + tất cả relay OFF
 ↓
App xóa profile cũ
```

Reset được xử lý trước kiểm tra version nên profile cũ dù đang có version lớn vẫn bị reset.

Khi thêm lại, App sync với:

```text
version = max(local_version, device_version) + 1
```

Sau reset, lần sync đầu tiên sẽ là `version=1`.

## 8. Serial Monitor

Baud rate:

```text
115200
```

Khi kết nối thành công:

```text
[MQTT] SUB control=OK security=OK config=OK topic=smarthome/device/relay01/config
[MQTT] CONNECTED
```

Khi nhận profile:

```text
[MQTT RX] topic=smarthome/device/relay01/config payload=...
[CONFIG] incoming_version=1 current_version=0
[PROFILE] Saved to NVS version=1
[CONFIG] Relay profile applied successfully
```

Khi schedule hoạt động:

```text
[SCENARIO] CH1 Đèn trần -> ON (18:00-23:00)
```

## 9. Lưu ý phần cứng

Firmware đang giả định relay board dùng logic:

```text
HIGH = ON
LOW  = OFF
```

Nếu relay board thực tế của bạn là active-low, đổi:

```cpp
#define RELAY_ON LOW
#define RELAY_OFF HIGH
```

Không đổi GPIO CH1/CH2 nếu đang sử dụng đúng wiring của `RelayWebTest.ino`.

## Camera MQTT IP Auto-Discovery Update

The ESP32-CAM reference firmware now reports its current DHCP IP through MQTT discovery/register and status messages.

The AppDemo automatically stores the camera `ip`, `stream`, `status_url`, `http_port`, and `stream_port` values. The Camera IP field in the Live Stream panel is read-only and is populated from MQTT; no manual IP entry is required.

Camera discovery uses live `announce/register` packets. Retained discovery packets are ignored for nearby-device detection. A retained camera status may update the IP of an already-added camera without creating a new nearby device.

The current camera stream remains HTTP MJPEG (`http://CAMERA_IP:81/stream`) in this update. RTSP is not enabled yet.
