# ESP32 Simulator

**IMPLEMENTED**, build PlatformIO `esp32dev` thành công. Bản build dùng cấu hình mẫu trống. PlatformIO báo RAM tĩnh 46.028 byte, Flash 797.761 byte. Firmware không được tải lên board trong lượt triển khai.

Một binary có thể dùng cho nhiều board; ID được gán qua NVS theo định dạng `ESP32-SIM-01` đến `ESP32-SIM-99`. Trạng thái boot ban đầu là `IDLE`, với nonce mới cho mỗi lần boot. Ứng dụng giữ ACK Serial, Wi-Fi và thống kê riêng theo COM. State machine tiếp tục xử lý lệnh dừng khi worker mạng đang chờ kết nối hoặc phản hồi.

Xem [cấu hình, build và nạp thủ công](../firmware/esp32_simulator/README.md). Kết quả giới hạn thực tế, GPIO stop, lỗi Wi-Fi và NVS qua reboot trên board thật đang ở `NOT_TESTED`.
