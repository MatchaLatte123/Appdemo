# Tổng kết Phase V3.1

| Hạng mục | Trạng thái |
|---|---|
| AI Server, MQTT/Video capture và Recorder | VERIFIED_LIVE trên V3 đang chạy, PID AI 12936 |
| Audit, readiness, giải thích anomaly, workflow model | IMPLEMENTED, VERIFIED_OFFLINE với 9/9 test |
| Phiên thực tế | 1 ACCEPTED, 0 REJECTED, 107 giây. 58 dòng lịch sử, trong đó 17 đủ điều kiện và 41 bị loại khi kiểm lại. Tổng cửa sổ bị lọc trước khi lưu chưa biết. |
| MQTT Broker | NEEDS_MORE_NORMAL_DATA. 17 cửa sổ phù hợp cho fit, mode unknown. |
| Video Server | NEEDS_MORE_NORMAL_DATA. 0 cửa sổ phù hợp, 2 có decoder gaps, mode recording:1. |
| Phiên bản model | Không có model active. Bản build f1d5fa7cb8104d9899c6e223731b8e0c chưa có artifact. |
| Statistical Detector và Isolation Forest | VERIFIED_OFFLINE trên fixture. Chưa có baseline đủ dữ liệu production. |
| Kiểm định thật và attribution | BLOCKED do thiếu phiên normal độc lập đạt gate và thiếu feed định danh MQTT. |
| Dashboard V3.1 | IMPLEMENTED, smoke test Tk đạt. API live chưa nạp code phase này. |

Các tệp đổi gồm adaptive_security.py, adaptive_profiles.py, adaptive_telemetry.py, adaptive_evidence.py, appdemo/adaptive_security_panel.py, tests/test_adaptive_security.py và 10 báo cáo phase_v31. Trước chỉnh sửa đã sao lưu sáu tệp mã/test vào Archive/phase_v31_prechange và xác minh lại manifest SHA-256 sau kiểm thử. Đường rollback model đã qua fixture. Rollback mã dùng sáu bản sao đã đối chiếu, sau khi operator cho phép nạp lại AI Server. Schema kho thêm bảng kiểm toán; baseline và session thực hiện có vẫn do tiến trình cũ giữ nguyên.

Phase này không yêu cầu AI Server, Broker hay Recorder restart. Không có lệnh bắt đầu, duyệt phiên hoặc kích hoạt trên production. Tiếp theo cần operator cho phép nạp V3.1, rồi quan sát và duyệt các phiên normal độc lập đủ điều kiện cho từng mode.
