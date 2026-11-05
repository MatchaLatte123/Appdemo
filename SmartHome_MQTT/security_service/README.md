# Smart Home Security Service — Phase 1

Dịch vụ thu thập dữ liệu độc lập, không thực hiện AI inference và không cô lập thiết bị.

## Thành phần
- MQTT collector: nhận status, telemetry, announce/register và security events.
- Network collector: Scapy capture metadata qua Npcap; lưu cửa sổ 5 giây, cập nhật mỗi giây.
- SQLite: `data/security.db`.
- Web dashboard/API: mặc định `http://127.0.0.1:8787` (cấu hình `0.0.0.0:8787` để truy cập từ LAN).

## Cài đặt trên Windows
1. Cài Python 3.11+.
2. Cài Npcap từ trang chính thức, bật chế độ tương thích WinPcap nếu trình cài đặt cung cấp.
3. Mở `config.json`; chỉnh MQTT host, interface (để trống dùng interface mặc định), IP camera nếu thay đổi.
4. Nếu broker đã bật authentication, điền `mqtt.username` và `mqtt.password` bằng tài khoản dịch vụ riêng. Không dùng tài khoản của ESP32.
5. Chạy `run_security_service.bat` bằng **Run as administrator** để Scapy/Npcap có quyền capture.
6. Mở `http://127.0.0.1:8787`.

## Chọn interface Npcap
Chạy PowerShell:
```powershell
py -c "from scapy.all import get_if_list; print('\\n'.join(get_if_list()))"
```
Copy tên interface phù hợp vào `network.interface`. Để trống nếu muốn Scapy chọn interface mặc định.

## Tài khoản Mosquitto và ACL

File `mosquitto_acl.example` ở thư mục này là mẫu phân quyền theo từng thiết bị và tài khoản collector. Tạo password file bằng `mosquitto_passwd` trên broker, sau đó cấu hình `password_file` và `acl_file` trong `mosquitto.conf`. Không dùng chung tài khoản giữa Relay, Fan, Camera, Collector và AppDemo. Collector chỉ cần quyền read.

**Chưa bật ACL mẫu ngay:** firmware hiện tại gọi MQTT connect bằng Client ID, chưa truyền username/password. Nếu bật `allow_anonymous false` trước khi cập nhật firmware, các ESP32 hiện tại sẽ mất kết nối. Cần triển khai theo trình tự: tạo tài khoản → cập nhật firmware nhận credential riêng → kiểm tra từng thiết bị → bật ACL/disable anonymous → kiểm thử AppDemo và collector.

## API
- `GET /api/status`
- `GET /api/summary`
- `GET /api/devices`
- `GET /api/events`
- `GET /api/network`
- `GET /api/collector-events`

## Lưu ý bảo mật và giới hạn
- Dashboard hiện chưa có đăng nhập. Chỉ bind LAN khi đã giới hạn Windows Firewall cho subnet quản trị; không port-forward ra Internet.
- MQTT service account cần quyền subscribe đúng các topic telemetry/status/discovery/security; không cấp quyền publish điều khiển hoặc security/quarantine.
- Bản hiện tại không tự tạo tài khoản Mosquitto, không sửa ACL, không thay firmware. Cần triển khai riêng sau khi xác minh `mosquitto.conf` và phiên bản broker.
- Packet capture trên switch thông thường chỉ thấy traffic tới/từ máy server, broadcast/multicast được chuyển tới NIC. Muốn thấy unicast giữa các thiết bị cần port mirroring/AP capture.
- Không lưu raw packet hay payload MQTT nguyên bản. JSON MQTT được lưu giới hạn 12 KB; cần rà soát payload thực tế trước khi đưa vào môi trường production.
- Chưa có AI, rule-based IDS, ngưỡng phân loại, tự động quarantine hoặc policy enforcement.
