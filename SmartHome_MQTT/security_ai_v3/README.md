# Smart Home Adaptive AI Security V3

AI Server dùng telemetry từ bộ thu Wireshark và Recorder để học hành vi của mạng, phát hiện bất thường và trình bày sự cố trong AppDemo. Các phiên normal cần được operator duyệt. Baseline và model đang hoạt động nằm trong SmartHomeAI/AdaptiveSecurity thuộc LocalAppData.

Start_Servers.cmd đọc servers.launch.json để khởi động AI Server và Recorder. Cấu hình AI nằm trong config/ai_server.json. Cấu hình rỗng dùng registry AppDemo và dữ liệu Recorder. Có thể cung cấp identity_bindings hoặc broker_sys khi đã có nguồn tin cậy.

Kiểm tra trực tiếp bằng lệnh python -B -m pytest -p no:cacheprovider -q security_ai_v3/tests. Cổng AI Server mặc định là 8767 trên loopback. Dashboard và operator dùng các endpoint V3 tại /api/security/adaptive/. Khuyến nghị chỉ hiển thị bằng chứng và thao tác để operator xem xét.
