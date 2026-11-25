# Dashboard AppDemo V3.1

Giám sát V3 hiện phần tổng quan số phiên đã duyệt, mẫu qua audit, mẫu bị loại, thời lượng học, trạng thái ứng viên và readiness từng dịch vụ. Chi tiết phiên truy vấn nhóm theo profile/mode, quality, khoảng thời gian, tỷ lệ quan sát feature và lý do loại. Chi tiết ứng viên và sự cố có metrics, expected ranges, current scores và khuyến nghị. Luồng start/stop/accept/build/activate/rollback dùng endpoint có bearer token.

Smoke test Tk ẩn xác nhận render tổng quan mới. Giao diện có fallback khi backend cũ chưa xuất trường audit/readiness. Bản đang chạy ở 8767 chưa nạp thay đổi V3.1, nên kết quả kiểm thử giao diện mới là VERIFIED_OFFLINE.
