# BASIC_10 và collector

Thứ tự đã đối chiếu API và artifact là `syn_flag_number`, `rst_flag_number`, `ack_flag_number`, `fin_flag_number`, `psh_flag_number`, `Min`, `Max`, `AVG`, `Std`, `IAT`.

| Nội dung | Implementation dùng chung |
|---|---|
| TCP flags | Số gói có cờ chia cho tổng số gói trong cửa sổ |
| Min, Max, AVG | Byte từ `frame.len`, giữ nguyên độ dài wire |
| Std | Độ lệch chuẩn mẫu, mẫu số n−1 |
| IAT | Khoảng thời gian từ gói đầu tới gói cuối, chia n−1, tính bằng giây |
| Scope | MQTT Broker, Video API hoặc endpoint camera riêng. Hai chiều và nhiều TCP connection có thể chung lane |
| Retransmission | Bộ thu có thống kê retransmission riêng; contract hiện dùng các gói TCP đủ metadata, không lọc mọi retransmission |
| Incomplete / lỗi capture | Dữ liệu pending chưa được chấm. Frame bị cắt, báo lost segment, metadata lỗi hoặc timestamp lùi có thể làm reset nhóm. Probe ghi discarded/pending |
| Tuổi cửa sổ | Worker hiện dùng 10 gói và giới hạn 30 giây theo config; probe 100 gói dùng để chẩn đoán |
| Tiền xử lý | Pipeline XGBoost hiện dùng median train cho missing values, không scale. Studio gọi transformer của artifact cho raw vector khớp |
 
Fixture TShark đọc offline 13 frame TCP, cho 1 cửa sổ 10 gói và 3 packet pending. Fixture 3 frame trả `WAITING_FOR_PACKETS`. Công thức live và offline packet dùng cùng probe. Tương đương với CIC CSV huấn luyện tiếp tục ở `BLOCKED_CLASS_CONDITIONED_GROUPING_AND_UNVERIFIED_EXTRACTOR`. Số prediction quan sát có thể thấp hơn số window hoàn thành khi API chỉ lưu window mới nhất.
