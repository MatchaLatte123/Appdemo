# Trạng thái định danh thiết bị

Runtime báo identity_source UNCONFIGURED. Registry AppDemo vẫn là nguồn metadata thiết bị. MQTT chưa có feed phiên xác thực từ broker gắn với username, client ID, session ID, IP/cổng và thời gian. Các nguồn MQTT trong phiên Idle chưa đủ điều kiện VERIFIED. Kết quả MQTT_BROKER được đánh giá ở cấp dịch vụ.

Camera có thể khớp endpoint theo cấu hình Recorder và hiện PROBABLE khi thiếu binding xác minh. VERIFIED yêu cầu mapping Recorder đáng tin cậy có hiệu lực trong cửa sổ và khớp registry. Resolver kiểm các trạng thái VERIFIED, PROBABLE, UNKNOWN, AMBIGUOUS bằng fixture. Không thay đổi ACL hoặc mật khẩu broker.
