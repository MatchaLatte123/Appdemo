# Tiền xử lý và chất lượng

Chunk mặc định 8 MiB qua Arrow. Mỗi file tạo shard Parquet zstd và checkpoint gồm hash nguồn/hash shard. Hash sai làm shard được tạo lại; nguồn đổi giữa khi đọc làm xử lý dừng. DuckDB chạy 2 luồng và 768 MiB, dùng spill giới hạn 16 GiB. Workspace thực tế khoảng 442 MB gồm shard và output nén.

Phát hiện 495 ô thiếu, 0 NaN hoặc invalid, 0 Inf và 2 ô số bất khả thi trong 10 predictor. Các bộ đếm theo ô không tương đương số dòng. Dòng bất khả thi, thiếu, sai cấu trúc hoặc scenario ngoài phạm vi bị loại; không điền zero. Sau label/numeric validation còn 42.927.262 dòng. 496 dòng đã parse bị loại bởi numeric/quality ngoài 82.284 dòng Recon. Dòng lỗi cấu trúc được đếm riêng.

Imputer median và StandardScaler hoặc RobustScaler fit trên train theo run. Tree mặc định không scale. Sampling train theo cap từng lớp và seed; weighted learning tùy chọn, mặc định không oversample.
