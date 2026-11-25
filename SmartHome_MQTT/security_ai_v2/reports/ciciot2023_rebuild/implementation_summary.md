# Tổng kết CICIoT2023 V2 Rebuild

| Gate | Kết quả |
|---|---|
| 1. Cô lập runtime | Snapshot V3 đã kiểm hash/integrity. V3 pause BLOCKED do tiến trình không có đường graceful đã xác minh. Broker, Video và V3 tiếp tục hoạt động. |
| 2. Kiểm kê | 219 CSV, 43.010.042 dòng đúng cấu trúc, 42.927.262 dòng đủ điều kiện trước dedup. |
| 3. Feature | BASIC_8/BASIC_10; audit train-only, khả năng dùng runtime bị chặn. |
| 4. Chuẩn bị dataset | PREPARED. 5.637.844 dòng, zero overlap của group/chữ ký theo phép kiểm; EXPLORATORY_FILE_GROUP_SPLIT. |
| 5. Studio | 127.0.0.1:8766; venv độc lập; đã chạy preflight từ dataset thật và sáu thuật toán selectable. |
| 6. Kiểm thử | 7/7 đạt. Train 6 thuật toán chỉ trên fixture. |
| 7. Sẵn sàng | Registry thật có 0 run; model CIC thật chưa train. Operator bắt đầu từ tab Training. |

Thay đổi nằm trong security_ai_v2/rebuild gồm data, train, contract, Studio API, UI và tests. Bổ sung launcher Start_Training_Studio.cmd, requirements-rebuild.txt, ignore rules và báo cáo. File được sinh tại work gồm inventory, manifest, checkpoint shard và ba split Parquet. Các thư mục .studio-venv và reference được cô lập khỏi runtime.

Launcher từ checkout: security_ai_v2\Start_Training_Studio.cmd --port 8766. Preset bắt đầu BASIC_8/QUICK_EXPERIMENT, 60.000 mẫu train/lớp, 30.000 validation, 2 luồng CPU, 2 GiB RAM, 30 phút. Tỷ lệ split thực tế sau dedup là 23,47/7,86/68,67. Giữ kết quả BENCHMARK_ONLY do class-conditioned grouping và thiếu xác minh parity. Không bật V2 inference.
