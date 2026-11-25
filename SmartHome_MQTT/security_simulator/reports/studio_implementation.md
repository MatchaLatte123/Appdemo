# Simulation Studio

Bảy tab PySide6 gồm Overview, Devices, Physical Tests, Offline Attack Tests, Feature Inspector, Evaluation và History. PyQtGraph hiển thị điểm attack theo cửa sổ quan sát cùng ngưỡng được đọc từ AI Server. Poll API, Serial và replay chạy ngoài GUI thread. Môi trường Studio cài độc lập tại `security_simulator/.venv`.

- Windows liệt kê COM5, COM6 và COM8. Trong lượt xác minh chỉ đọc danh sách cổng. Kiểm thử fixture kiểm tra protocol/version, ID trùng, mất kết nối, reboot và lệnh Stop.
- SQLite lưu phiên, danh tính board và IP tại từng lần lấy mẫu, dữ liệu dự đoán đã khử trùng, trạng thái sức khỏe và kết quả replay. Phần History xuất đồng thời CSV, JSON và HTML vào thư mục ID phiên.
- Smoke test Qt offscreen đã đọc trạng thái AI, chạy replay, lưu SQLite và hiển thị History. Các bước này đạt `VERIFIED_OFFLINE` hoặc xác minh tích hợp chỉ đọc với dịch vụ thật; thao tác board thật vẫn `NOT_TESTED`.
