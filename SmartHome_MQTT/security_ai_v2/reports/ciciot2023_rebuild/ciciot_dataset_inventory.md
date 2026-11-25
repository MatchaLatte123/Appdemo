# Kiểm kê CICIoT2023

Nguồn local là E:/DACNTT/SmartHome_MQTT/Appdemo/CIC-IoT-2023. Có 219 CSV, tổng 8.178.848.544 byte. Đã đọc 43.010.042 dòng đúng cấu trúc và loại 3 dòng sai số cột. Header danh sách đầy đủ và SHA-256 theo file nằm trong work/inventory.json và work/manifest.json. SHA-256 của manifest khóa là c4b1aafe3ca062d14ed0ab0e6ef14af58b33f81b331bd7e5c60e711deb989183.

CSV local có 39 cột, thiếu cột label. Đã xác nhận đủ 10 trường đề xuất. Khảo sát nguồn local tìm thấy CSV và tài liệu, chưa thấy PCAP đồng nguồn để kiểm parity. Feature-quality audit định lượng trên 10 predictor và trường chẩn đoán Number. Các cột khác được kiểm kê schema. Nguồn chỉ đọc; SHA-256 kiểm tra trước và sau xử lý.
