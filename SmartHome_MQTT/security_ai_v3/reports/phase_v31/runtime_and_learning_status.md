# Trạng thái runtime và học bình thường

Kiểm tra chỉ đọc ngày 24/11/2026, múi giờ Asia/Saigon. API 8767 trả BEHAVIORAL_V3 với PID 12936. Capture SHARED ở trạng thái ACTIVE. MQTT Broker PID 4868 và Recorder PID 21328 tiếp tục lắng nghe. Recorder HEALTHY. Kho phiên ở IDLE, enforcement false.

Live đã có 1 phiên Idle được operator chấp thuận, 0 phiên bị loại ở cấp phiên. Phiên kéo dài 107 giây và dùng schema behavior-v3.1. Phiên bản thử build f1d5fa7cb8104d9899c6e223731b8e0c có trạng thái NEEDS_MORE_NORMAL_DATA, hash null. Không có artifact hoặc version hoạt động.

Trong phase này chỉ đọc trạng thái live, kho phiên và phiên bản. Tiến trình hiện hành chưa nạp bản cập nhật V3.1. Start, stop, review, accept/reject, build và activate được kiểm trong SQLite tạm.
