# Tương thích collector

| Tài sản | Kết quả |
|---|---|
| MQTT Broker | BLOCKED_CLASS_CONDITIONED_GROUPING_AND_UNVERIFIED_EXTRACTOR |
| Video Server / Camera | BLOCKED_CLASS_CONDITIONED_GROUPING_AND_UNVERIFIED_EXTRACTOR |

Extractor offline nhận metadata TShark, scope theo IP/cổng dịch vụ và endpoint camera, bỏ capture bị cắt, gom cùng một kích thước nhóm cố định cho mọi window của từng dịch vụ. Tính tỷ lệ TCP flag, thống kê frame length và IAT. Fixture xác minh công thức trên cả hai dịch vụ. Target, endpoints và timestamp chỉ xuất metadata.

Neto et al. mô tả workflow CSV gom 10 packet với Benign và 100 với DoS/DDoS. Runtime không có nhãn trước để chọn nhóm. Phạm vi host/TCP còn khác tập CIC gồm nhiều giao thức. Báo cáo live MQTT trước đây ghi 3–14 frame mỗi 5 giây, không đủ để suy ra phân phối đầu vào của CIC. Không có đánh giá parity live và không bật inference. Cần PCAP CIC có provenance hoặc dữ liệu cùng topology được trích bằng policy label-agnostic cố định trước khi xét tích hợp. [Bài báo CICIoT2023](https://doi.org/10.3390/s23135941).

## Đối chiếu với ba model BASIC_10 mới nhất

Đã đối chiếu toàn bộ thứ tự 10 cột của LightGBM 464a77a4, XGBoost 70aea805 và Random Forest 9f495053. Probe dùng chung metadata với LiveMonitor và CaptureBranch. Luồng MQTT Broker, Video API và từng endpoint camera được gom riêng. Probe chạy song song hai cửa sổ cố định 10 và 100 gói, loại khối chưa đủ khi phiên thu kết thúc, làm sạch khối đang gom khi có dấu hiệu mất gói hoặc timestamp đi lùi. Probe chỉ giữ vector gần nhất và các bộ đếm của từng luồng/cỡ cửa sổ.

Kiểm lại 1.098.191 dòng Benign có dữ liệu số. AVG × Number khớp Tot sum và tỷ lệ cờ TCP nằm trên lưới 1/Number ở toàn bộ các dòng đã kiểm. Trong 32 dòng Number = 2 có Min khác Max, Std đều khớp độ lệch chuẩn mẫu với mẫu số N−1, và không dòng nào khớp độ lệch chuẩn tổng thể. Bộ thu đã sửa từ độ lệch chuẩn tổng thể sang độ lệch chuẩn mẫu. Chưa có cặp PCAP và CSV cùng gói để kiểm lớp byte của Min/Max hoặc semantics IAT của exporter. Số gói vẫn khác theo lớp ở nguồn huấn luyện, theo tài liệu CIC và cột Number local.

| Thử thụ động 22 giây | Gói quan sát | Cửa sổ 10 | Cửa sổ 100 |
|---|---:|---:|---:|
| MQTT Broker | 40 | 4 | 0 |
| Video API | 0 | 0 | 0 |
| Camera | 3.410 | 341 | 34 |

Đây là kiểm tra thu metadata, không có ground truth cùng nguồn để xác định độ khớp hoàn toàn. Có cửa sổ MQTT/camera cho Min = 54 byte, nên không suy diễn cùng miền kích thước với CSV. Chưa có bằng chứng bắt được phiên Video API trong đoạn này. 32/32 kiểm thử đạt, gồm vector và công thức trên hai cỡ, phân tuyến, chất lượng capture và xác thực API.

Cấu hình cic_input_probe bật cho lần khởi động AI Server kế tiếp. Endpoint được bảo vệ bằng token tại /api/security/adaptive/collector-features, với decision_eligible=false. Phiên thử chạy riêng ở chế độ quan sát ngắn với mã mới. AI Server đang phục vụ trên cổng 8767 chưa được nạp phiên bản này, Broker và Recorder vẫn chạy. Không có score hoặc quyết định CIC được tạo.

## XGBoost shadow trên AI Server

AI Server cổng 8767 đã nạp XGBoost run 70aea805b30a4c1cb918b6e95b3c1d0f (dataset cic-rebuild-2, BASIC_10). File cấu hình security_ai_v3/config/ai_server.json khóa artifact theo SHA-256. Worker chạy với thư viện khớp môi trường model, một luồng CPU và nhận vector float32 qua pipe.

Chỉ cấu hình 10 gói/cửa sổ cho chấm điểm shadow. AI Server lấy cửa sổ hoàn tất và mới từ từng luồng, giữ bộ đếm và loại điểm khi dữ liệu quá cũ hoặc capture bị chặn theo chất lượng. Endpoint GET /api/security/adaptive/xgboost-shadow yêu cầu bearer token. Ngưỡng 0.006729660090059042 được ghi là ngưỡng benchmark, không có quyền ra quyết định. Gate tương thích tiếp tục BLOCKED và decision_eligible=false.

AppDemo hiển thị tab XGBoost · Quan sát với trạng thái, điểm, gói/cửa sổ và giờ nhận. Xác minh trực tiếp sau triển khai có điểm MQTT Broker và camera, Video API đang chờ cửa sổ. Cả 34 bài kiểm thử V2 và V3 đạt. Smoke test cửa sổ Tk ẩn đọc trực tiếp từ endpoint đang chạy và hiển thị điểm hợp lệ. AI Server tiếp tục monitoring. XGBoost shadow chưa tạo cảnh báo hoặc đóng góp vào quyết định của Adaptive.
