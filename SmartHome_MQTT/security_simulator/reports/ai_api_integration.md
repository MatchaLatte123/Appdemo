# Tích hợp API hiện có

Studio gọi GET tài nguyên `xgboost-shadow`, `collector-features`, `overview`, `devices` và `incidents` dưới `/api/security/adaptive/`. API trả 401 khi thiếu Bearer token trên phép thử chỉ đọc. Tích hợp có token đã đọc được model, threshold, enforcement, trạng thái worker và các lane. Token được đọc từ tệp operator của V3, không ghi vào lịch sử hay báo cáo.

Mỗi mẫu dự đoán ghi lại run ID, SHA-256, scope, thời gian window, số packet, điểm, ngưỡng, nhãn, capture quality, input parity, `decision_eligible` và enforcement. Collector query và prediction query được thực hiện riêng. Raw vector chỉ đi kèm khi cặp timestamp, số gói và scope khớp chính xác. Processed vector dùng bước tiền xử lý của artifact đã đối chiếu hash.

API không cấp flow ID, thời điểm suy luận riêng cho từng cửa sổ hoặc danh tính nguồn của dự đoán. Studio ghi `null` cho flow ID và ước lượng tuổi cửa sổ tại tick worker được lấy mẫu. Không thêm API và không khởi động lại dịch vụ.
