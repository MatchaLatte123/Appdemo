# Đánh giá quy nguồn

Firmware ID và COM cho biết board mà operator đã quản lý. Địa chỉ IP trong phản hồi Serial là bằng chứng do board báo. Studio so khớp địa chỉ theo thời gian với **observed service peers** trong API Devices và đưa bằng chứng sự cố V3 vào nhóm tham chiếu riêng. API có thể báo `VERIFIED` identity của một peer trong telemetry V3; điều đó không xác định peer nào thuộc cửa sổ XGBoost đã chấm.

Studio ghi `UNKNOWN` cho nguồn của prediction, precision để trống khi chưa có đề xuất được xác minh. Phân biệt board tham gia, peer quan sát, suspected source trong V3 incident, verified identity, và dịch vụ bị ảnh hưởng. Cửa sổ chứa nhiều TCP connection cần quy nguồn cấp window trước khi đánh giá đúng/sai cho từng thiết bị. `isolation_assessment` vẫn lấy từ API hiện tại với trạng thái `UNVALIDATED`.
