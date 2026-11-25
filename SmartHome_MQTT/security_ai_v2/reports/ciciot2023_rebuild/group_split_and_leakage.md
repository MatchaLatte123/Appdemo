# Group split và kiểm soát trùng

Không có ID attack run đã xác minh. Chia theo SHA-256 file (file giống hệt cùng nhóm đầy đủ). Trạng thái EXPLORATORY_FILE_GROUP_SPLIT. Stratify theo scenario khi có thể. Các scenario một hoặc hai nguồn không đủ để tuyên bố test độc lập theo scenario.

| Split | Trước dedup | Sau dedup | Tỷ lệ sau dedup |
|---|---:|---:|---:|
| Train | 30.295.578 | 1.322.935 | 23,47% |
| Validation | 6.337.731 | 443.147 | 7,86% |
| Test | 6.293.953 | 3.871.762 | 68,67% |

File-group split trước dedup bám gần 70/15/15. Giữ test trước rồi validation rồi train là quyết định bảo vệ holdout khi một chữ ký chạm nhiều split. Các chữ ký lẫn nhãn bị loại hoàn toàn. Có 193.651 chữ ký xuất hiện nhiều split trước lọc và 0 sau lọc. Gần-trùng được định nghĩa bằng làm tròn flags 0,01 và length 0,1 byte trên BASIC_8 rồi hash 64-bit. Vì BASIC_8 là tập con của BASIC_10, cùng hàng rào áp dụng cho cả hai. Các hàng chỉ gần nhau nhưng khác bin vẫn có thể còn. Có 2.206.597 chữ ký BASIC_8 exact khác nhau trong tập đã qua numeric validation.

Không giao nhau theo group ID đã quan sát. Không thể xác minh các file khác nhau có phải cùng capture run hay không. Dedup làm thay đổi mạnh phân phối split và không tái cân bằng validation/test sau khi khóa. Manifest và ba Parquet có SHA-256. Test chỉ mở cho một run được operator khóa sau khi chọn trên validation.

## Dataset cic-rebuild-2

Workspace mới security_ai_v2/work_70_15_15 đọc cùng 219 CSV và giữ nguyên gán nhóm nguồn của cic-rebuild-1. Pipeline so sánh các cách phân bổ chữ ký nhóm trùng để giữ nhiều mẫu nhất trong các phương án đã thử. Chữ ký lẫn nhãn vẫn bị loại. Sau khi gán chữ ký, lấy mẫu theo seed 42 và cắt 11.013 dòng để mỗi lớp đạt đúng 70/15/15.

| Split | Tổng | Normal | Attack | Tỷ lệ |
|---|---:|---:|---:|---:|
| Train | 6.636.238 | 460.922 | 6.175.316 | 70% |
| Validation | 1.422.051 | 98.769 | 1.323.282 | 15% |
| Test | 1.422.051 | 98.769 | 1.323.282 | 15% |

Đã xác minh số dòng trong metadata Parquet, SHA-256 của ba split, 0 nhóm CSV giao nhau và 0 chữ ký giao nhau theo quy tắc lượng tử hóa đã nêu. Manifest mới có SHA-256 b049fb6de2952cbe51424d0dfb165ca68f6b022b55ecaaba04e0810197d29286. Tập test mới chưa được chấm. Nguồn test vẫn là các nhóm CSV trước đây nên kết quả mới không được coi là một kiểm định độc lập với các kết quả đã quan sát từ bản trước. Manifest, test Parquet và khóa test cũ đã đối chiếu hash và không đổi.
