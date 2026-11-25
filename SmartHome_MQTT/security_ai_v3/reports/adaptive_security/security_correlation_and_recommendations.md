# Đối chiếu bằng chứng và khuyến nghị

Policy adaptive-policy-1 dùng độ lệch theo feature, mức duy trì qua cửa sổ, hoạt động theo IP quan sát và tình trạng broker/recorder. Một lỗi vận hành có thể mở SERVICE_DEGRADATION mà không gán đối tượng tấn công. Điểm Isolation Forest được biểu diễn dưới dạng anomaly score.

Phân loại gồm BEHAVIOR_ANOMALY, SERVICE_DEGRADATION, SUSPECTED_MQTT_FLOOD, SUSPECTED_NETWORK_FLOOD, SUSPECTED_VIDEO_HTTP_OVERLOAD và MULTI_SOURCE_OVERLOAD. Nhãn nhiều nguồn cần đồng thời nhiều IP có hoạt động đáng kể, đặc trưng tải tăng và dịch vụ ghi nhận ảnh hưởng. IP không xác nhận thiết bị hay thực thể độc lập.

Policy sử dụng LOW cho anomaly không thấy ảnh hưởng, MEDIUM khi có ảnh hưởng và HIGH khi có bằng chứng nhiều nguồn và capture đạt OBSERVED. INFO và CRITICAL chưa được cấp trong policy hiện tại. Chất lượng và giới hạn được lưu với incident.

Incident có ID, profile version, timestamps, bằng chứng, dedup theo subject, mở sau ba cửa sổ, hồi phục sau bốn cửa sổ sạch và dùng lại ID trong cooldown 60 giây. Mất input không được dùng làm bằng chứng phục hồi.

Khuyến nghị có target, bằng chứng, điều kiện, rủi ro, thao tác hoàn tác và yêu cầu duyệt. Việc đánh giá giới hạn theo MQTT client chỉ xuất hiện khi có đúng một binding VERIFIED trong trường hợp phù hợp. V3 không có endpoint thay đổi firewall, broker hoặc thiết bị.
