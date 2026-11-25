# Huấn luyện và kiểm định baseline

Mỗi profile cần tối thiểu bốn phiên ACCEPTED độc lập theo thời gian. Hai phiên trở lên dùng fit với ít nhất 60 mẫu. Hai phiên muộn dành riêng cho hiệu chỉnh và kiểm định, mỗi phiên có tối thiểu 36 mẫu chấm được. Kiểm định yêu cầu ít nhất 90% đầu vào của phiên đạt điều kiện và tỷ lệ cửa sổ normal bị đánh dấu bất thường không quá 5%.

Fit lưu median, MAD, p01/p99, dải tham chiếu và hệ số sàn theo đơn vị. Isolation Forest dùng seed và giới hạn mẫu cố định theo mã. Ngưỡng học từ phiên hiệu chỉnh normal. Báo cáo candidate có phân phối điểm normal, số mẫu, khoảng thời gian, missingness và cửa sổ bất thường trên giờ quan sát. Tỷ lệ theo cửa sổ chưa phải tỷ lệ incident trên ngày ở hệ thống thật.

Artifact có phiên bản, thời điểm, schema, bản scikit-learn và SHA-256. Chỉ artifact có hash/schema hợp lệ mới được kích hoạt. Inference dùng model bất biến trong một lượt xử lý. Candidate không tự kích hoạt; có thể quay lại phiên bản RETIRED. Các artifact fixture chỉ nằm trong thư mục test tạm.

Trạng thái triển khai thật là NEEDS_MORE_NORMAL_DATA. Chưa có model V3 production được fit.
