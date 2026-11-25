# Video API qua Ethernet

**IMPLEMENTED** qua N05, N06 và N08. **NOT_TESTED** trên board vật lý trong lượt này. Kiểm tra HTTP 200 từ máy Studio chỉ chứng minh Recorder trả lời máy đó; không chứng minh ESP32 đã đi qua Ethernet.

1. Nạp simulator trên board test. Đặt địa chỉ LAN và port Recorder trong cấu hình board. `NORMAL_HTTP` gọi GET `/health` và giữ lại mã HTTP cùng số lần thành công.
2. Chọn đúng một board, mở N05 với lane `VIDEO_API`, sau đó **Start one board**. Giữ riêng N07 cho lane camera.
3. Đối chiếu mã 2xx trong telemetry board, peer trong API V3, số gói pending/completed, cửa sổ 10 gói, capture quality và nhãn. Ghi rõ ISO UTC và ID phiên. Nếu không tạo cửa sổ, giữ trạng thái chờ, không suy diễn nhãn.
4. Khi cần nhiều client, lặp N06 với danh tính riêng và khởi động từng board thủ công. Dùng N08 để quan sát một client ngoài firmware đã được operator cấp quyền.

Recorder gọi camera theo đường media endpoint riêng. Client đến `/health` thuộc lane Video API. AppDemo truy cập Recorder cùng máy có thể dùng loopback và không tạo gói qua card Ethernet. Không dùng một trong các luồng này để xác minh thay cho luồng khác.
