# Kế hoạch kiểm thử vật lý

Chọn đúng số board theo tình huống. Trạng thái triển khai của toàn bộ bảng là **IMPLEMENTED**, kết quả vật lý hiện **NOT_TESTED**.

| Mã | Luồng | Điều kiện và thao tác |
|---|---|---|
| N01 | MQTT Broker | 1 board có Wi-Fi, giữ IDLE và quan sát |
| N02 | MQTT Broker | 1 board, start normal telemetry |
| N03 | MQTT Broker | Từ 2 board có ID riêng, start normal telemetry lần lượt |
| N04 | MQTT Broker | 1 board, start legitimate reconnect |
| N05 | Video API | 1 board, `/health` trên IP LAN của Recorder |
| N06 | Video API | Từ 2 client, start normal HTTP từng board |
| N07 | Camera | Chọn lane camera đang hoạt động, quan sát thụ động |
| N08 | Video API | Ghi client bên ngoài, thời gian và quyền truy cập trong operator notes |
| S01 | MQTT Broker | 1 board, 2 thao tác MQTT đúng giao thức |
| S02 | MQTT Broker hoặc Video API | Từ 2 board, thao tác bình thường từng board, giữ provenance controlled |
| R01 | Luồng cần kiểm tra | 1 board, recovery 15 giây; đối chiếu Serial và dịch vụ ở cuối phiên |

Ghi lại snapshot service trước/sau, các lần đọc Serial và cửa sổ đủ điều kiện. Chỉ chọn **Operator verified the expected activity was normal** khi đã đối chiếu bối cảnh. Kết quả vẫn mô tả lane, vì cửa sổ có thể trộn nền mạng. S01 và S02 giữ nhãn `CONTROLLED_PROTOCOL_TEST`; R01 ghi trạng thái hồi phục và trường hợp lệnh dừng không được ACK.

Để kiểm tra an toàn, operator có thể ngắt USB, sử dụng **Stop selected** hoặc kéo GPIO27 về GND trên riêng board test. Quan sát giới hạn cục bộ và xác nhận boot về IDLE. Thực hiện xong chọn History để xuất báo cáo. Các cửa sổ lấy mẫu sau lúc mở phiên dùng để thống kê; số cửa sổ chưa bắt kịp ở API được ghi riêng, không coi là packet drop đã xác minh.
