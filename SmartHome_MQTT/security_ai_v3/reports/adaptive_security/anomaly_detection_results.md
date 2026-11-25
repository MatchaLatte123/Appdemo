# Kết quả kiểm tra anomaly

## VERIFIED_OFFLINE

Bộ kiểm thử AI Security đạt 93 test và 4 subtest. Sáu test V3 chạy lại sau lần chỉnh tích hợp cuối đều đạt.

- Replay chỉ đọc NormalData5.pcap bằng TShark và hook LiveMonitor. Đọc 14.912 frame IPv4/TCP thuộc broker đã chọn, 26/26 cửa sổ hoàn tất khớp đếm frame theo epoch, tính được publish_rate ở cả 26 cửa sổ. Chạy offline mất 1,74 giây ở lượt đo này. PCAP không được đưa vào tập fit V3.
- Fixture kiểm ngưỡng thiếu dữ liệu, chia phiên độc lập, phát hiện tăng packet/publish, từ chối hash/schema không khớp, auth API, mất chất lượng capture, freeze phiên học, dedup, phục hồi và rollback.
- Policy mở sự cố sau ba cửa sổ liên tiếp. Fixture kiểm tra mở đúng một sự cố, rồi phục hồi sau bốn cửa sổ hợp lệ.

## VERIFIED_LIVE tại lớp telemetry

Phiên quan sát thụ động có thời lượng capture 22 giây đọc 37 frame MQTT và 3.919 frame Video. Recorder HEALTHY ở 25 lượt đọc. Camera, MQTT device, broker và Video Server đều có cửa sổ UNVERIFIED; có cửa sổ LOSS cần xem lại. Không dùng phiên này để fit hoặc làm ground truth.

## Cần thêm dữ liệu

Chưa đo false incidents/ngày, độ trễ phát hiện với sự kiện thật, chi phí tài nguyên khi có nhiều profile hay khả năng phát hiện DoS/DDoS tải thấp. API health có tick latency và RSS tiến trình AI cho các phiên dài. Cần các phiên normal được xác nhận độc lập và ground truth phù hợp trước khi đánh giá các số liệu này.
