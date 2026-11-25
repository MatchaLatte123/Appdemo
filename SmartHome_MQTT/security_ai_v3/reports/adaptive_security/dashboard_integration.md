# Dashboard AppDemo

AppDemo hiện dùng Tkinter. Tab Giám sát V3 gồm Thiết bị và nguồn, Sự cố, Phiên học và Baseline. Tổng quan có số thiết bị bình thường hoặc bất thường, sức khỏe hai dịch vụ, số incident và baseline đang dùng. Mở chi tiết để xem giá trị feature, lịch sử, chất lượng, attribution, profile và khuyến nghị. Giao diện đọc trực tiếp endpoint V3 và làm mới mỗi hai giây.

Các thao tác phiên học, build, kích hoạt và rollback gọi API local bằng token cùng tài khoản Windows. Thao tác build chạy nền. Thông tin chi tiết hiển thị dạng JSON để giữ cấu trúc các trường evidence. AppDemo không cung cấp nút cô lập mạng.

Đã khởi tạo được cửa sổ Tk và tab V3 trong bài kiểm tra ẩn.
