# Kiểm chứng normal vật lý

**NOT_TESTED**. Windows có ba cổng COM nhưng chưa xác minh simulator firmware trên từng cổng. Không nạp firmware hoặc chủ động khởi động phiên. Kiểm tra TCP Broker và HTTP Recorder từ host đều thành công.

Mốc tham chiếu từ đợt idle trước gồm MQTT 8/10 mẫu normal nhận đúng, camera 15/15, Video API chưa có prediction Ethernet. Để so sánh kết quả mới, giữ run ID, ngưỡng, lane và loại phiên giống nhau. Báo cáo vật lý sẽ ghi tỷ lệ cảnh báo trên cửa sổ quan sát, cảnh báo/giờ, score, trạng thái recovery và giới hạn quy nguồn. Attack recall và độ trễ phát hiện tấn công chưa có dữ liệu vật lý để tính.
