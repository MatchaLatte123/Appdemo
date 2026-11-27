# IoT Simulation Studio

Studio Windows đọc dự đoán XGBoost live từ AI V2 và xem lại báo cáo đã lưu.

## Khởi động

Tạo môi trường Python riêng nếu chưa có:

```powershell
py -3.13 -m venv security_simulator/.venv
& security_simulator/.venv/Scripts/python.exe -m pip install -r security_simulator/requirements.txt
```

Chạy [Start_Servers.cmd](../Start_Servers.cmd) để bật AI V2 và Recorder, sau đó mở [Start_Simulation_Studio.cmd](Start_Simulation_Studio.cmd). Địa chỉ, cổng và mô hình được đọc từ [servers.launch.json](../servers.launch.json).

```powershell
& security_simulator/.venv/Scripts/python.exe -B -m security_simulator.studio.main --diagnose
```

## Theo dõi và xem báo cáo

- **Tổng quan** hiển thị tình trạng dịch vụ và dự đoán mới nhất theo luồng.
- **Live → Dashboard** theo dõi tốc độ bắt gói Ethernet, tổng gói, dung lượng và tiến độ tạo cửa sổ.
- **Live → Thiết bị nguồn** hiển thị điểm theo IP peer. Tên theo hồ sơ IP là gợi ý.
- **Đặc trưng live** hiển thị vector và giá trị sau tiền xử lý. **Đánh giá** hiển thị biểu đồ điểm live và kết quả của báo cáo được chọn.
- **Lịch sử & xuất báo cáo** mở các phiên đã lưu và xuất CSV, JSON, HTML.
