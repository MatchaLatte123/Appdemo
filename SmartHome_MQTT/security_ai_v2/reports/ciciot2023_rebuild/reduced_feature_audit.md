# Audit BASIC_8 và BASIC_10

BASIC_10 giữ syn_flag_number, rst_flag_number, ack_flag_number, fin_flag_number, psh_flag_number, Min, Max, AVG, Std, IAT. BASIC_8 loại AVG và IAT. Kiểm tra tương quan chỉ dùng reservoir 25.000 dòng TRAIN. AVG tương quan Pearson 0,865 với Std và 0,721 với Max. IAT bị loại để giảm phụ thuộc vào định nghĩa thời gian/gom gói chưa đối chiếu được. BASIC_10 giữ làm mốc so sánh. Cả hai preset có feature order cố định.

Năm flag nằm trong [0,1] và có thể biểu diễn tỷ lệ. Min, Max, AVG, Std là các thống kê kích thước; bản extractor đề xuất dùng byte trên dây. IAT đề xuất dùng giây giữa các packet. Công thức candidate là công thức thực thi được, còn units, lớp giao thức, padding và aggregation của extractor CSV chưa được tái xác minh bằng PCAP đồng nguồn. Không feature nào constant hoàn toàn trong mẫu TRAIN. Min 60 chiếm khoảng 81,5% mẫu. Không dùng IP, port, Number, nhãn, filename, timestamp hoặc device ID làm predictor.

Number nhận giá trị 10 ở 1.097.547 dòng Benign, 100 ở 7.837.909 dòng DoS và 33.949.982 dòng DDoS thuộc các file đủ điều kiện. Những giá trị này chứng minh khác biệt về kích thước nhóm trong nguồn local và được loại khỏi predictor. Rủi ro mô hình học dấu vết quy trình tạo tập vẫn tồn tại ngay cả khi bỏ Number.
