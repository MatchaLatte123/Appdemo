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

## Continuous Camera Recording Service

The independent recorder is in `recording_service/`. It connects to the existing
ESP32-CAM HTTP/MJPEG endpoint and writes H.264 MP4 segments. AppDemo does not
start or stop this service.

### Requirements and setup

- Install FFmpeg for Windows and ensure both `ffmpeg.exe` and `ffprobe.exe` are
  available on `PATH` (the binaries are normally distributed together).
- Reserve a fixed DHCP address for ESP32-CAM, or update `stream_url` in
  `recording_service/config.json` whenever its address changes. The default URL
  reflects the camera address in the checked-in AppDemo profile.
- Start the service manually from `SmartHome_MQTT`:

  ```powershell
  python -m recording_service
  ```

The service records each enabled camera even when no viewer is connected. It
keeps one FFmpeg process and one camera input per camera, retries with
exponential backoff, and cuts five-minute MP4 files from that continuous input
without reconnecting the camera at each segment boundary. It writes segments
under `recording_service/footage/` and enforces
`max_storage_gb` across that footage directory. Finished files are named `.mp4`;
interrupted or unverified files retain the `.partial.mp4` suffix and are omitted
from footage listings.

The local status endpoint is `http://127.0.0.1:8765/api/status`. Valid footage
can be listed with `http://127.0.0.1:8765/api/footage?camera_id=ESP32-CAM-01`
and a local date/time window, for example:

```text
/api/footage?camera_id=ESP32-CAM-01&date=2026-10-17&start_time=14:00&end_time=15:00&utc_offset_minutes=420
```

The window is half-open `[start, end)`: segments ending at `start` or beginning
at `end` are excluded. Stored timestamps are UTC; query wall-clock values use
the server's local time unless `utc_offset_minutes` is supplied. Results include
`segments` and explicit `gaps`, so a camera outage is not presented as
continuous footage. The prototype catalog is `recording_service/footage.sqlite3`
and survives service restarts. Logs are written to
`recording_service/logs/recording_service.log` with rotation.

Protect or unprotect a completed segment with
`POST /api/footage/{recording_id}/protection` and JSON body
`{"is_protected": true}`. Export a requested time interval by posting
`{"device_id":"ESP32-CAM-01","start_time":"2026-10-17T07:00:00Z","end_time":"2026-10-17T08:00:00Z"}`
to `/api/export`. The server trims and re-encodes intersecting segments with
FFmpeg; picture gaps are represented as black video in the exported interval.
The response contains a `download_url` under `/api/exports/{export_id}`.

The Camera Server API also exposes:

```http
GET    /api/status
POST   /api/live-sessions              {"device_id":"ESP32-CAM-01"}
GET    /api/live-sessions/{session_id}/stream.mjpg
DELETE /api/live-sessions/{session_id}
GET    /api/footage?device_id=ESP32-CAM-01&start_time=2026-10-17T07:00:00Z&end_time=2026-10-17T08:00:00Z
GET    /api/recordings/{recording_id}
GET    /api/recordings/{recording_id}/file
GET    /api/playback/manifest?device_id=ESP32-CAM-01&start_time=2026-10-17T07:00:00Z&end_time=2026-10-17T08:00:00Z
POST   /api/export                     {"device_id":"ESP32-CAM-01","start_time":"2026-10-17T07:00:00Z","end_time":"2026-10-17T08:00:00Z"}
GET    /api/exports/{export_id}
DELETE /api/exports/{export_id}
```

Timestamp query values must be timezone-aware ISO 8601. Footage and playback
manifest responses contain ordered segments and explicit gaps. MP4 file routes
support single HTTP byte ranges for seeking. Live sessions return a
server-relative MJPEG URL. The recorder copies the original JPEG frames from
the same FFmpeg camera input used for recording, then relays them to viewers
without transcoding or opening another camera connection. Slow viewers receive
the newest available frame instead of accumulating a growing delay. A live
session expires after eight hours if it is not explicitly deleted.

Example requests:

```sh
curl -s http://127.0.0.1:8765/api/status
curl -s -X POST http://127.0.0.1:8765/api/live-sessions \
  -H 'Content-Type: application/json' -d '{"device_id":"ESP32-CAM-01"}'
curl -H 'Range: bytes=0-1023' \
  http://127.0.0.1:8765/api/recordings/00000000-0000-4000-8000-000000000001/file
```

Example responses (fields abbreviated):

```json
{"service":"running","cameras":[{"camera_id":"ESP32-CAM-01","camera_status":"recording","recorder_state":"recording","last_media_at":"2026-10-17T07:15:03+00:00"}],"storage":{"used_bytes":123456789,"max_bytes":5368709120,"cleanup_target_bytes":4831838208,"warning":null}}
```

```json
{"session_id":"00000000-0000-4000-8000-000000000099","camera_id":"ESP32-CAM-01","stream_url":"/api/live-sessions/00000000-0000-4000-8000-000000000099/stream.mjpg","transport":"mjpeg","expires_at":"2026-10-17T15:15:00+00:00"}
```

```json
{"device_id":"ESP32-CAM-01","segments":[{"recording_id":"00000000-0000-4000-8000-000000000001","start_time":"2026-10-17T07:00:00Z","end_time":"2026-10-17T07:05:00Z","playback_url":"/api/recordings/00000000-0000-4000-8000-000000000001/file"}],"gaps":[]}
```

Storage usage and the configured limit remain visible, but quota changes and
automatic cleanup are temporarily disabled. The AppDemo control is disabled and
`POST /api/storage/settings` returns HTTP 503. Existing footage is not removed
by the storage manager while this switch is off.

AppDemo no longer opens the ESP32 MJPEG URL directly because the current
firmware serves only one HTTP stream client. Its camera card polls the local
recorder status API while MQTT discovery, online status, and security controls
remain in use. The API is independent of Tkinter and does not require Flutter
or the production database.

### AppDemo Camera Center

Install the AppDemo dependencies, then run the Camera Server and AppDemo in
separate PowerShell windows. Start the server first and keep its configured
HTTP endpoint at `http://127.0.0.1:8765` for the default AppDemo setting:

```powershell
cd .\SmartHome_MQTT
python -m pip install -r .\appdemo\requirements.txt
python -m recording_service
```

In another window:

```powershell
cd .\SmartHome_MQTT\appdemo
python .\mqtt_test_console.py
```

On a camera device card, choose **CAMERA CENTER: LIVE / PLAYBACK**. Refresh
Server/Storage, choose a configured camera, then press **Live**. Close it with
**Đóng Live**; this deletes only the viewing session. For playback, enter a
date and start/end time, load the footage timeline, and press **Play khoảng
giờ**. The Server exports the selected range to one MP4, so the progress bar,
seek, pause, 10-second skip, and speed selection operate across segment
boundaries. Gap rows identify missing time; the exported MP4 shows those spans
as black video. AppDemo asks the API to remove its temporary export after
playback stops; it does not access the Server's filesystem. AppDemo decodes server URLs with OpenCV and displays frames
through Pillow; it never opens the camera's MQTT-advertised stream URL.

### Camera Center smoke-check scenarios

1. With the recorder running, open Camera Center, refresh status/storage, select
   the camera and start Live. Confirm frames appear and the status says LIVE.
2. Close Live. Confirm the view stops, then refresh Server/Storage and verify
   the recorder remains `recording` and storage usage continues to update.
3. Pick a recorded one-hour interval such as 14:00–15:00. Load the timeline,
   confirm segment rows are chronological, then play, pause, seek, skip ±10s,
   and change speed across a segment boundary.
4. Test an interval containing a known camera outage. Confirm a GAP row appears
   and playback presents the gap as black video. Test an empty interval and
   confirm the UI reports that no footage exists.
5. Stop the Camera Server or disable the camera recorder. Confirm Server/API
   errors and camera offline states appear without freezing the main AppDemo
   window or changing MQTT relay/fan operation.

To run independently of AppDemo after Windows user sign-in, register the
current-user Scheduled Task in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\recording_service\install_recording_task.ps1
Start-ScheduledTask -TaskName SmartHomeCameraRecording
```

Use `uninstall_recording_task.ps1` to stop and remove the task. This user-level
task starts at logon (without requiring an elevated/system service install),
restarts after process failures, and ignores duplicate launches.
