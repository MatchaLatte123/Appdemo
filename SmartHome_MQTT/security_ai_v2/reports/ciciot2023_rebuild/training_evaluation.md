# Đánh giá training

Gate 1–7 chỉ chuẩn bị và kiểm thử. Preflight đọc preset thật BASIC_8 với 120.000 dòng train và 30.000 dòng validation, chưa gọi fit. Chạy 7 test đạt, gồm discovery, source integrity, label lọc, numeric quality, group leakage, imputer/scaler, tất cả thuật toán với fixture, threshold validation, khóa test, registry artifact, export, cancel và API loopback. Kết quả fixture là bằng chứng phần mềm.

CIC thật chưa có chỉ số benchmark. Threshold chọn từ validation theo FPR limit. QUICK báo EXPERIMENTAL. FULL_TRAINING dùng toàn validation nếu đạt giới hạn bộ nhớ. Operator chọn và khóa một run rồi mới đọc test; test chỉ tính tại ngưỡng đã chọn. Eval báo confusion matrix, specificity, attack recall, F1/macro F1, ROC/PR-AUC, kịch bản, nhóm và độ trễ batch trên dòng.
