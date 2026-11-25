# Chất lượng phiên normal

Phiên Idle có 58 dòng được lưu bằng cơ chế trước V3.1. Tất cả ghi nhận UNVERIFIED. Kiểm tra lại bằng quy tắc V3.1 cho kết quả sau.

| Nhóm | Mode | Dòng lưu | Đủ điều kiện có xét nhãn operator | Loại trong các dòng đã lưu |
|---|---|---:|---:|---|
| MQTT Broker | unknown | 17 | 17 | 0 |
| Video Server | recording:1 | 2 | 0 | 2 do decoder gaps |
| MQTT Device | online | 37 | 0 | 37 thiếu định danh VERIFIED |
| Camera Device | recording | 2 | 0 | 2 do decoder gaps |

Tổng kiểm tra lại là 17 đủ điều kiện và 41 loại. Bộ thu cũ chỉ ghi dòng qua bước lọc sơ bộ, nên không thể tính tổng số cửa sổ đã bị loại trước khi lưu. Phiên này thiếu kiểm toán đầy đủ cho calibration hoặc validation.

V3.1 lưu audit của cả cửa sổ đạt và bị loại, lý do loại, bằng chứng packet/drop/decoder, feature completeness, UTC, dịch vụ, mode, trạng thái dịch vụ và provenance. Dữ liệu lỗi được loại, feature thiếu giữ null, và UNVERIFIED chỉ vào build khi operator đã duyệt phiên. Summary của phiên nêu audit_complete để phân biệt lịch sử với audit đầy đủ.
