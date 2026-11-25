# Khôi phục và cô lập runtime

Đã chụp snapshot SQLite online qua SQLite backup API và kiểm tra PRAGMA quick_check. Cấu hình AI, launcher và manifest SHA-256 ở Archive/v3_pre_cic_rebuild. Không tìm thấy artifact joblib V3 trong state runtime. Đã sao chép Studio cũ và ba tệp UI từ Archive làm nguồn tham chiếu trong security_ai_v2/reference. Archive gốc chỉ được đọc.

V3 PID 12936 phục vụ GET 8767 BEHAVIORAL_V3. Recorder PID 21328 và MQTT Broker PID 4868 vẫn chạy. V3 dùng Python trực tiếp và có Dumpcap/TShark riêng. Recorder đang dùng Python base qua môi trường cũ và có FFmpeg riêng. Studio dùng .studio-venv độc lập, lắng nghe 127.0.0.1:8766.

V3 chạy theo launcher Windows không cửa sổ, mã đang chạy chỉ xử lý KeyboardInterrupt và thiếu giao diện graceful shutdown đã kiểm chứng. Tạm dừng bị BLOCKED theo điều kiện của task; giữ PID V3 thay vì terminate. Không thay launcher V3. Rollback của Studio là đóng riêng Studio; V3, Recorder, Broker và SQLite gốc tiếp tục dùng đường cũ.
