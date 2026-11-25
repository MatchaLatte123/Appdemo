# Export và trạng thái runtime

Registry xuất model.joblib chứa estimator, imputer và scaler khi dùng, feature_contract.json, dataset_manifest.json, evaluation.json và SHA-256. Report có phiên bản package, feature order, công thức đề xuất, binary mapping, threshold và thông tin lượng dữ liệu đã sử dụng. Đã kiểm export/load và sửa artifact để xác nhận bị phát hiện trong fixture.

Model thật chưa được train hoặc export. BENCHMARK_ONLY là trạng thái duy nhất. MQTT và Video đều BLOCKED cho runtime vì Class-conditioned grouping và chưa xác minh feature parity. Tích hợp sau cần cùng extractor label-agnostic trên dữ liệu phù hợp, đối chiếu phân phối và chất lượng đầu vào, rồi đánh giá shadow normal độc lập trên từng dịch vụ. Studio không chứa chức năng deploy hay quarantine.
