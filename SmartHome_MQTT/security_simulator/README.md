# IoT Simulation Studio

Bộ kiểm thử độc lập cho XGBoost BASIC_10 của V2 đang chạy trên AI Server V3. Giao diện Windows dùng tiếng Anh. Studio đọc cấu hình triển khai và API operator của AI Server hiện tại. Firmware simulator dùng cho các board ESP32 test do operator chọn.

## Khởi động trên Windows

Tại thư mục gốc SmartHome_MQTT, tạo môi trường riêng nếu chưa có:

```powershell
py -3.13 -m venv security_simulator/.venv
& security_simulator/.venv/Scripts/python.exe -m pip install -r security_simulator/requirements.txt
```

Mở [Start_Simulation_Studio.cmd](Start_Simulation_Studio.cmd). Để kiểm tra kết nối mà không mở COM, chạy:

```powershell
& security_simulator/.venv/Scripts/python.exe -B -m security_simulator.studio.main --diagnose
```

Studio lấy đường dẫn launcher, cổng MQTT, vị trí SQLite và nhịp đọc từ [studio.example.json](config/studio.example.json). Có thể sao chép thành `config/studio.local.json` hoặc truyền `--config`. URL AI, địa chỉ Ethernet, port video và model lấy từ cấu hình triển khai. Token operator được đọc tại `%LOCALAPPDATA%/SmartHomeAI/AdaptiveSecurity/operator.token`.

## Chạy phiên vật lý

1. Chuẩn bị và tự nạp [firmware simulator](firmware/esp32_simulator/README.md) lên từng ESP32 được dành cho kiểm thử.
2. Trong **Devices**, bấm **Scan COM**, chọn cổng của board simulator, rồi **Connect selected COM**. Gán một NVS ID riêng cho từng board bằng **Set device ID**. Xác nhận mỗi board phản hồi handshake, Wi-Fi có IP, và ở `IDLE`.
3. Chọn hàng của một hoặc nhiều board trong **Devices**. Sang **Physical Tests**, chọn kịch bản, luồng và thời gian. Ghi chú nguồn ground truth, rồi bấm **Open observation**.
4. Với kịch bản có workload, chọn đúng một hàng **Devices** rồi bấm **Start one board**. Lặp lại thao tác này độc lập cho từng board nếu phiên cần nhiều nguồn. N01, N07 và N08 là quan sát thụ động.
5. Dùng **Stop selected**, **Close observation** hoặc **Stop all**. Có thể kéo GPIO27 xuống GND trên board simulator để dừng cục bộ. Nếu USB mất kết nối, lịch làm việc cục bộ vẫn có giới hạn.
6. Xem **Feature Inspector**, **Evaluation** hoặc chọn phiên trong **History** để xuất CSV, JSON và HTML.

## Demo DoS/DDoS offline

Mở **Offline Attack Tests**, để trống đường dẫn để lấy Parquet TEST đã ghim, chọn DOS, DDOS hoặc ALL, rồi bấm **Run offline**. Chọn tên scenario nếu cần lọc một kịch bản nguồn. Có thể chọn PCAP/PCAPNG cục bộ theo hướng dẫn trong [báo cáo replay](reports/offline_dos_ddos_evaluation.md). Nút replay chỉ đọc file và chạy suy luận cục bộ.

## Trạng thái xác minh

Build firmware, kiểm thử phần mềm, replay và tích hợp đọc API đã thực hiện. Handshake thật, MQTT/HTTP từ board và khả năng nhìn thấy gói Video API trên Ethernet cần operator chạy sau khi nạp đúng board. Xem [tổng kết](reports/implementation_summary.md) và [kế hoạch phiên vật lý](reports/physical_test_plan.md).
