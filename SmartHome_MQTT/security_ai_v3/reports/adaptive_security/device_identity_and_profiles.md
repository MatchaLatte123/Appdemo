# Danh tính và profile thiết bị

V3 đọc registry AppDemo và cấu hình camera của Recorder. IP/cổng là metadata quan sát. Camera khớp endpoint duy nhất theo cấu hình được đánh dấu PROBABLE. Nguồn không khớp là UNKNOWN, binding trùng hoặc đổi trong cửa sổ dẫn đến AMBIGUOUS.

Để VERIFIED với MQTT, cấu hình identity_bindings trỏ đến JSON do nguồn quản trị tin cậy xuất. Mỗi bản ghi cần source BROKER_AUTH_SESSION, device_id đã đăng ký, ip, port, start và end theo Unix epoch seconds, username, client_id, session_id được broker xác thực. Binding phải phủ trọn cửa sổ. Username và Client ID từ bản tin tự khai không phải nguồn xác minh.

Camera cần VERIFIED_RECORDER_MAPPING, endpoint ip/port, thời hạn, device_id trong registry, camera hiện diện trong tình trạng Recorder và ánh xạ duy nhất theo cấu hình. File nguồn nên do cùng tài khoản vận hành quản lý và ghi atomically. AI Server đọc lại khi tệp hoặc registry thay đổi. Báo BINDINGS_INVALID và hạ định danh khi tệp lỗi. Chưa có exporter phiên xác thực tại broker được cấu hình ở máy hiện tại.

API tách SUSPECTED_SOURCE, AFFECTED_DEVICE và AFFECTED_SERVICE. Profile dùng thiết bị VERIFIED hoặc dịch vụ. Fallback theo loại chỉ xuất hiện khi có kiểm tra ít nhất một thiết bị khác tập fit và vượt cổng validation. Tình trạng camera lấy từ Recorder, các ngữ cảnh dịch vụ gồm số camera ghi hình hoặc dải số client khi có $SYS.
