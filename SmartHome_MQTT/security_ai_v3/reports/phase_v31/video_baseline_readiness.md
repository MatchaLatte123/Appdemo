# Readiness Video Server

NEEDS_MORE_NORMAL_DATA. Phiên Idle lưu hai cửa sổ recording:1, cả hai có cờ decoder gaps và bị loại theo chính sách V3.1. Recorder báo HEALTHY tại thời điểm kiểm tra. Tình trạng Recorder và độ tin cậy capture được báo riêng.

V3.1 thêm camera_fps ở cấp Video Server từ median FPS thực đo của các camera đang recording. Giá trị chưa đo là null. http_rate giữ null khi decoder chưa quan sát được request. Cần thêm phiên operator ghi nhận riêng cho streaming, recording, playback trên AppDemo và biến thiên bitrate. Readiness tách theo số camera đang recording.
