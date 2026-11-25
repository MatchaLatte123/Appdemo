# Phiên học bình thường

1. Bắt đầu trong AppDemo, ghi bối cảnh quan sát. Trạng thái LEARNING_NORMAL.
2. Dừng phiên, xem số mẫu, coverage theo vai trò, chất lượng và trường thiếu. Phiên ở PENDING_REVIEW.
3. Duyệt ACCEPTED nếu xác nhận hệ thống hoạt động bình thường. Ghi lý do duyệt. REJECTED loại toàn bộ khỏi candidate.
4. Tạo candidate khi đã có các phiên theo yêu cầu. Kích hoạt là thao tác operator riêng.

SQLite WAL tại thư mục SmartHomeAI/AdaptiveSecurity trong LocalAppData lưu phiên, vector, chỉ số capture, bối cảnh và bằng chứng duyệt. Phiên tối đa bốn giờ hoặc giới hạn mẫu mỗi phiên. Mẫu có dấu hiệu bất thường hoặc ảnh hưởng dịch vụ bị loại khỏi phiên học theo dịch vụ. Phiên còn mở khi tắt AI được đưa về PENDING_REVIEW lúc khởi động lại. Mẫu và phiên hoàn tất có retention 30 ngày. Sự cố đã phục hồi cũng được giữ 30 ngày. Các phiên đang dùng để lượng hóa false alerts nên bao phủ thiết bị online/offline, bật/tắt ghi hình và reconnect hợp lệ.

Chất lượng UNVERIFIED được ghi rõ trong review và chỉ có thể vào fit sau khi operator xác nhận phiên. LOSS và UNAVAILABLE không vào tập huấn luyện. Sự tồn tại của cửa sổ live không tự tạo nhãn NORMAL.
