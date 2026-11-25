# Phân bố nhãn

Tên scenario được suy ra từ folder và tiền tố filename đã đối chiếu taxonomy. Provenance INFERRED_FOLDER_FILENAME. Không coi là nhãn độc lập trong từng dòng hoặc identity thiết bị.

| Family | Binary | Hợp lệ trước dedup | Sau dedup giữa split |
|---|---|---:|---:|
| BENIGN | NORMAL | 1.098.126 | 625.308 |
| DOS | ATTACK | 7.845.025 | 1.216.564 |
| DDOS | ATTACK | 33.984.111 | 3.795.972 |

Loại toàn bộ 82.284 dòng Recon-PortScan. Chỉ allowlist name theo CIC cho Benign, 4 DoS và 12 DDoS. Label, family, scenario, source name/hash, group và split chỉ là metadata. Danh sách theo CSV và scenario có trong manifest và được hiển thị ở Dataset.
