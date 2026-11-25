# Tiến độ Adaptive AI Security V3

| Gate | Trạng thái |
|---|---|
| A — Telemetry | IMPLEMENTED. VERIFIED_OFFLINE trên 26 cửa sổ MQTT. VERIFIED_LIVE cho thu MQTT/Video thụ động và đọc Recorder. Nguồn xác thực MQTT còn thiếu. |
| B — Learning | IMPLEMENTED, VERIFIED_OFFLINE. NEEDS_MORE_NORMAL_DATA cho candidate thực. |
| C — Detection | IMPLEMENTED, VERIFIED_OFFLINE. Detector chạy trên fixture và bỏ chấm khi thiếu baseline/feature. Production NOT_TRAINED. |
| D — Evidence | IMPLEMENTED, VERIFIED_OFFLINE. Khuyến nghị dựa trên dữ liệu và có giới hạn attribution. |
| E — Integration | VERIFIED_LIVE. Cổng 8767 phục vụ BEHAVIORAL_V3; bộ thu chạy và Recorder phản hồi. |
| F — Validation | Replay bắt gói và fixture nghiệp vụ đã kiểm. Chưa đạt đánh giá false incidents/ngày và độ trễ trên dữ liệu triển khai. |

Launcher và Dashboard chỉ dùng V3. Mã và tài liệu V2 được lưu ngoài checkout tại Archive/security_ai_v2, Training Studio V2 đã dừng. Recorder hiện tại tiếp tục chạy. V3 ở trạng thái NOT_TRAINED và chỉ bắt đầu học khi operator mở và duyệt các phiên normal.
