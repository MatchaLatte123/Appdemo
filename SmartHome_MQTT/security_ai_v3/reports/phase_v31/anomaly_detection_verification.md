# Kiểm chứng phát hiện bất thường

VERIFIED_OFFLINE. Replay chỉ đọc NormalData5.pcap bằng TShark, qua bộ thu V3. Đọc 14.912 frame, khớp số frame ở 26/26 cửa sổ hoàn tất. PCAP không đưa vào tập fit.

Fixture kiểm anomaly tồn tại qua nhiều cửa sổ, một sự cố được giữ ID qua nhiều cửa sổ, capture LOSS không phát sinh nhãn tấn công và không bị tính là recovery. Kiểm recovery sau dữ liệu hợp lệ, rollback, schema/hash mismatch, phân tách mode và từ chối validation thiếu audit hoặc có phiên chồng lấn. Đạt 9 test.

Evidence bổ sung profile/version, current và expected ranges, điểm thống kê, điểm Isolation Forest, ngưỡng, số cửa sổ liên tiếp, ảnh hưởng dịch vụ, trạng thái attribution và khuyến nghị. Điểm là độ bất thường, không xác nhận thiết bị bị xâm nhập hoặc chứng minh khả năng phát hiện DoS/DDoS trên mạng thật.
