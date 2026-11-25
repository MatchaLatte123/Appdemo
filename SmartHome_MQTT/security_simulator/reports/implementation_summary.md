# Kết quả triển khai · 24/11/2026

| Hạng mục | Trạng thái |
|---|---|
| ESP32 firmware, NVS ID, USB serial, network worker và giới hạn | IMPLEMENTED, PlatformIO BUILD PASSED |
| Multiple COM, duplicate ID, timeout, reboot, Stop | VERIFIED_OFFLINE qua fixture; enumerate 3 COM thật, chưa handshake simulator vật lý |
| AI Server, model và threshold | Xác minh API đọc-only trên runtime hiện tại. MQTT TCP mở, Recorder HTTP 200. |
| PySide6 Studio, Feature Inspector, quản lý phiên | IMPLEMENTED; Qt offscreen smoke test đọc API, replay và cập nhật History |
| BASIC_10, SQLite, CSV/JSON/HTML | VERIFIED_OFFLINE |
| Offline DoS/DDoS | VERIFIED_OFFLINE trên 1.536 dòng Parquet TEST. Một báo nhầm Benign, 0 bỏ sót lớp Attack trong mẫu này. |
| PCAP | VERIFIED_OFFLINE bằng fixture Benign, 10 packet hoàn thành và 3 packet pending |
| Video API Ethernet từ ESP32, multi-board, recovery | NOT_TESTED trên phần cứng |
| Source attribution và isolation | UNKNOWN / UNVALIDATED. Enforcement vẫn false. |

Kiểm thử Studio 10/10. Regression V3 và CIC Rebuild 39/39. AppDemo hiện tại đã đọc và hiển thị lại worker và lane qua smoke test Tk ẩn. File model, ngưỡng, ACL, firewall, firmware đang vận hành và các tiến trình hiện có không được chỉnh sửa.

Bắt đầu tại [README và launcher](../README.md). Sau khi operator tự nạp simulator và chỉ định board trên COM, chạy N05 để xác minh Video API Ethernet, rồi chạy N03 cho nhiều nguồn MQTT.
