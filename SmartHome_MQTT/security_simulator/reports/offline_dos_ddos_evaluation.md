# Đánh giá DoS/DDoS offline

**VERIFIED_OFFLINE** với Parquet TEST được đối chiếu SHA-256 từ manifest và artifact đang được runtime sử dụng. Studio chọn 512 Benign, 512 DoS, 512 DDoS tại ngưỡng MQTT 0,01184. Kết quả `FEATURE_LEVEL_REPLAY` và `REUSED_TEST_BENCHMARK`.

| Chỉ số | Kết quả |
|---|---:|
| Confusion matrix `[[TN, FP], [FN, TP]]` | `[[511, 1], [0, 1024]]` |
| Normal FPR | 0,1953% |
| DoS recall | 512/512 |
| DDoS recall | 512/512 |
| Precision trên tập đã lấy mẫu | 99,9024% |
| F1 trên tập đã lấy mẫu | 99,9512% |

[Báo cáo JSON](studio_offline_result.json) có số liệu theo scenario. [Báo cáo 10 vòng](../../security_ai_v3/reports/xgboost_offline_test.json) là một phép đo khác với seed khác. Nhãn và tỷ lệ lớp của các dòng không đại diện cho incident độc lập hoặc tỷ lệ lớp trong mạng nhà.

### Dữ liệu người vận hành chọn

- Parquet phải có cùng hash với TEST đã ghim. CSV cần cột BASIC_10 theo tên hợp đồng, `y`, `family`, `scenario`. Số liệu CSV được tính theo nhãn do operator cung cấp với trạng thái `OPERATOR_LABELS_HOLDOUT_UNVERIFIED`.
- Với PCAP/PCAPNG, chọn lane, địa chỉ IPv4 được bảo vệ trong file, family và scenario gốc. Cổng và endpoint được đối chiếu theo model đang chạy. Studio dùng TShark đọc file với giới hạn dung lượng, frame và thời gian, sau đó tạo cửa sổ qua CIC probe.
- Nhãn PCAP ở cấp capture được giữ riêng, không suy diễn ground truth cho từng window. Fixture PCAP Benign đã xác minh đường đọc và suy luận; chưa chạy trên PCAP DoS/DDoS có bằng chứng nhãn cửa sổ.
