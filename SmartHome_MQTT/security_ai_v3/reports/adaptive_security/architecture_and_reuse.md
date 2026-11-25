# Kiến trúc Adaptive AI Security V3

V3 dùng một HTTP server trên loopback và lấy metadata từ cùng luồng Dumpcap/TShark đang phục vụ AI Server. Cả capture gộp lẫn capture tách nhánh gọi cùng một bộ gom cửa sổ V3. Recorder được đọc qua GET /health. Broker $SYS là nguồn tùy chọn, mặc định chưa cấu hình.

- adaptive_telemetry gom feature và đối chiếu danh tính.
- adaptive_profiles quản lý đặc trưng, chuẩn hóa, detector thống kê và Isolation Forest.
- adaptive_evidence đối chiếu tín hiệu dịch vụ, đánh giá rủi ro và tạo khuyến nghị.
- adaptive_security quản lý phiên học, model, incident và API adapter.
- ai_server và AppDemo chỉ phục vụ pipeline V3.

Runtime mặc định là quan sát. Hành vi, danh tính và ảnh hưởng đều có trường bằng chứng riêng.
