# Đặc trưng hành vi

Schema behavior-v3.1 tách khỏi CIC và MQTT XGBoost. API /api/security/adaptive/schema công bố kiểu, đơn vị, công thức, nguồn, cửa sổ, quy tắc thiếu và chất lượng cho từng trường.

| Feature | Đơn vị | Công thức và nguồn |
|---|---|---|
| packet_rate, byte_rate | frame/s, byte/s | Frame và byte trên dây hai chiều chia 5 từ capture |
| connection_rate | SYN/s | SYN có ACK tắt hướng đến cổng được bảo vệ chia 5, có thể gồm truyền lại |
| rst_rate | frame/s | RST hai chiều chia 5 |
| packet_rate_change | frame/s | Chênh lệch packet_rate giữa hai cửa sổ liên tiếp |
| publish_rate, subscribe_rate | message/s | PUBLISH và SUBSCRIBE inbound đã giải mã chia 5 |
| http_rate | request/s | HTTP inbound đến Video Server đã giải mã chia 5 |
| clients, message_rate, broker_health | client, message/s, boolean | Gauge, delta counter hoặc trạng thái kết nối observer được phép đọc |
| media_age, camera_fps | giây, frame/s | Tuổi media gần nhất, FPS do recorder xuất |
| ffmpeg_health, recorder_error | boolean | Khả dụng worker hoặc lỗi recorder gần đây và lỗi storage hiện hành |

Cửa sổ 5 giây căn theo epoch UTC. Tóm tắt 60 giây tính trung bình 12 cửa sổ liên tiếp đủ quan sát và cùng operating mode. API trả null cho trường chưa quan sát được. Tóm tắt bị gián đoạn khi đổi mode hoặc thiếu cửa sổ. Feature bắt buộc cần đủ ở đầu vào. Feature tùy chọn có thể được điền median trong vector model sau kiểm tra tỷ lệ trường hiện diện.

Các profile là MQTT_DEVICE, CAMERA_DEVICE, MQTT_BROKER và VIDEO_SERVER. VIDEO_PEER chỉ dùng quan sát. Biến số predictor không chứa IP, client ID, username, mã thiết bị hoặc tên tệp.

Phạm vi hiện tại là IPv4/TCP tại host đã cấu hình. Trường publish/subscribe có thể thiếu khi MQTT chưa được giải mã, có TLS hoặc thiếu đoạn TCP. Bộ gom đánh dấu các chỉ số frame/byte là UNVERIFIED khi capture chưa có bằng chứng độc lập về zero drops. LOSS và UNAVAILABLE bị loại trước khi fit. Tốc độ reconnect thực, lỗi xác thực, HTTP timeout counter và CPU/RAM của từng dịch vụ chưa có nguồn được xác minh.
