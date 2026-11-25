# Tích hợp AI Server V3

Launcher dùng security_ai_v3.ai_server và security_ai_v3/config/ai_server.json. GET /api/security/dashboard trả mode BEHAVIORAL_V3 và trạng thái monitoring để kiểm tra tiến trình trên loopback.

GET /api/security/adaptive/ hỗ trợ overview, health, schema, devices, devices/{key}, incidents, incidents/{id}, recommendations, profiles, profiles/{id}, learning, learning/{id}. POST /api/security/adaptive/operator/ hỗ trợ start, stop, accept, reject, build, activate và rollback. API chi tiết và thao tác dùng bearer token trong LocalAppData.

Có thể bật broker_sys trong cấu hình với host, port, tls, username_env và password_env cho tài khoản broker đã có quyền đọc $SYS. Khi thiếu nguồn, các feature tương ứng là null. Capture và Recorder không dựa vào model hoặc dataset ngoài.
