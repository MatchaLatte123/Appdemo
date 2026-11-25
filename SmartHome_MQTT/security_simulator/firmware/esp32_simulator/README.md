# ESP32 Simulator Firmware

Target `esp32dev`, PlatformIO, Arduino framework, PubSubClient 2.8.0. Bản build bằng file mẫu để trống SSID và thông tin xác thực. Hãy tạo cấu hình riêng trước khi build cho board test.

## Build và nạp thủ công

Sao chép `include/config.example.h` sang `include/simulator_config.h`. Điền SSID, địa chỉ IPv4 LAN của broker và Recorder, cùng thông tin tài khoản MQTT dành riêng cho kiểm thử. Broker của dự án yêu cầu đăng nhập. Tài khoản cần quyền publish đúng `smarthome/simulator/<ID>/telemetry`. Kiểm tra quyền với người quản trị trước khi thử MQTT. Dự án simulator không thay ACL.

Từ thư mục gốc dự án:

```powershell
pio run -d security_simulator/firmware/esp32_simulator
pio device list
pio run -d security_simulator/firmware/esp32_simulator -t upload --upload-port COMx
```

Thay `COMx` bằng cổng board simulator mà operator đã xác nhận. Các board dùng chung firmware và được gán ID riêng qua NVS. Sau khi kết nối bằng Studio, chọn **Set device ID** và nhập `ESP32-SIM-01`, `ESP32-SIM-02` hoặc ID khác chưa sử dụng.

## Điều khiển và giới hạn

Serial 115200 dùng `HELLO`, `STATUS`, `SET_ID <id>`, `START <mode>`, `STOP`, `RESET`. Device ID và nonce khởi động xuất hiện trong phản hồi. Các thao tác MQTT/HTTP chạy trên worker có watchdog. Boot về `IDLE`. Mất Wi-Fi hoặc lỗi ứng dụng kết thúc workload và cần lệnh khởi động mới.

- `NORMAL_MQTT` gửi telemetry hợp lệ vào namespace riêng. `NORMAL_RECONNECT` thực hiện tối đa ba lượt kết nối MQTT có chủ đích. `BOUNDED_PROTOCOL_TEST` thực hiện tối đa hai lượt MQTT hợp lệ. Telemetry dùng MQTT QoS 0, nên lần publish thành công phía ESP32 chưa phải xác nhận broker đã chuyển bản tin tới subscriber.
- `NORMAL_HTTP` chỉ GET `/health` của Recorder được cấu hình, không theo redirect và không đọc video hoặc tải body.
- Các workload cách nhau tối thiểu ba giây giữa hai lần xếp thao tác, giới hạn tối đa tám thao tác và 45 giây mỗi lượt, nghỉ tối thiểu 15 giây giữa các lượt. `RECOVERY` quan sát trạng thái Wi-Fi trong 15 giây.
- `STOP` qua Serial hoặc kéo GPIO27 xuống GND ngừng lịch gửi mới. Thao tác đã bắt đầu kết thúc theo giới hạn socket và watchdog. Khi giữ GPIO27 ở mức thấp, firmware từ chối lệnh chạy mới.
