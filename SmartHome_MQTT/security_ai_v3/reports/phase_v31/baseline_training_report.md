# Huấn luyện và quản lý baseline

Production NOT_TRAINED. Không fit hoặc kích hoạt model bằng dữ liệu thật trong phase này. Một phiên đã duyệt không đủ cho fit, calibration và validation.

Cổng V3.1 phân nhóm theo profile và mode, bảo toàn thứ tự thời gian. Cần ít nhất hai phiên fit với tổng 60 mẫu, một phiên calibration với 36 mẫu chấm được và một phiên validation muộn hơn với 36 mẫu chấm được. Phần thiếu audit chặn calibration/validation. Chỉ threshold được hiệu chỉnh ở phiên calibration. Rolling 60 giây và độ đổi packet rate dùng lịch sử trong đúng phiên, chặn khoảng trống.

Báo cáo ứng viên ghi hash tập dữ liệu và từng phiên theo profile, feature order, scaler robust, MAD và quantile, ngưỡng, seed/cấu hình detector, schema và hash artifact. Kích hoạt và rollback là lệnh operator riêng. Fixture kiểm cả hai detector, version/hash và rollback. Drift từ chuỗi 24 cửa sổ đã chấm được chỉ xuất bằng chứng và gợi ý thu phiên mới.
