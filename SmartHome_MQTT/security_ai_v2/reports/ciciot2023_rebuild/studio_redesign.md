# Studio local

Từ checkout, chạy security_ai_v2\Start_Training_Studio.cmd --port 8766, rồi mở http://127.0.0.1:8766. Studio đã được bật và xác minh trạng thái PREPARED. Nếu instance hiện tại đang lắng nghe, mở URL trực tiếp.

Studio dùng .studio-venv tách riêng. Để cài lại dependencies, tạo môi trường với python -m venv security_ai_v2\.studio-venv và chạy pip install -r security_ai_v2\requirements-rebuild.txt bằng Python của môi trường đó. Launcher chỉ chạy Studio và không khởi động worker trước thao tác operator.

Dataset liệt kê status, số lượng, hash file và split. Features cung cấp BASIC_8/BASIC_10 và audit train-only. Training nhận thuật toán, tham số, scaling, class weight, threads, memory, timeout và cap. Một worker chạy tại một thời điểm, có progress, log đĩa và cancel. Comparison dùng validation. Evaluation có ROC/PR, confusion matrix, thresholds validation và kết quả theo nhóm/kịch bản. Registry kiểm hash và xuất artifact. Muốn chọn dataset mới sau khi manifest đã khóa, chạy launcher với --work đường dẫn mới riêng.

Preset thử đề xuất BASIC_8, QUICK_EXPERIMENT, seed 42, 2 CPU threads, 2.048 MiB RAM, 60.000 mẫu train mỗi lớp, 30.000 validation và timeout 1.800 giây. Chọn thuật toán hoặc batch rồi nhấn Train. FULL_TRAINING dùng toàn validation, có thể dùng train giới hạn theo cấu hình. Cổng 8766 chỉ bind loopback.

Smoke test Edge headless xác nhận trang hiển thị PREPARED và các lựa chọn thuật toán qua API live. Không gửi lệnh Train.

## Đánh giá tự động

Studio tự ghi kết quả validation cho mỗi model và chọn model tốt nhất trong batch theo Attack Recall, FPR rồi F1. Trong FULL_TRAINING, tùy chọn Tự chạy Test sau huấn luyện được bật mặc định. Studio khóa test cho model được chọn và chấm với ngưỡng đã xác định trên validation. QUICK_EXPERIMENT tự xuất validation. Nếu Test đã có lượt chấm đầu, job mới tự chấm validation và chỉ chấm lại khi bật Dùng lại Test cho lượt tự động. Có thể tắt tự test để chọn run thủ công trước khi khóa.

Trong Đánh giá, bật Cho phép dùng lại tập Test để chấm lại cùng run hoặc một run FULL khác. Mỗi lần lưu lịch sử riêng và được đánh dấu REUSED_TEST_BENCHMARK. Khóa và báo cáo lần đầu được giữ lại. Kết quả chấm lại sử dụng cùng Test và ngưỡng từ validation của từng run.
