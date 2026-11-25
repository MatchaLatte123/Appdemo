# Khảo sát triển khai · 24/11/2026

Đối chiếu [báo cáo V2](../../security_ai_v2/reports/ai_v2_deployment_2026-11-24.md), [launcher](../../servers.launch.json), [cấu hình AI](../../security_ai_v3/config/ai_server.json), [AppDemo](../../appdemo/adaptive_security_panel.py), [bộ thu](../../security_ai_v3/live_monitor.py), [probe](../../security_ai_v3/cic_input_probe.py), [Training Studio](../../security_ai_v2/rebuild/studio.py) và [firmware ESP32 tham chiếu](../../platformio_fan/platformio.ini). Firmware mới là project riêng.

| Kiểm tra | Kết quả |
|---|---|
| Capture interface | Ethernet, theo launcher |
| MQTT Broker | TCP 1883 mở |
| Video Server | `GET /health` 8765 trả HTTP 200 |
| Training Studio V2 | Loopback 8766 trả HTTP 200 khi lập báo cáo triển khai |
| AI Server V3 | Loopback 8767, `MONITORING` |
| XGBoost | Worker `READY`, run `70aea805b30a4c1cb918b6e95b3c1d0f`, dataset `cic-rebuild-2` |
| Artifact và ngưỡng | SHA-256, feature order, window size và ngưỡng khớp API và config |
| Artifact parameters | n_estimators 150, max_depth 6, learning_rate 0.07 |
| Quyết định | `decision_eligible=false`, `enforcement=false` |

Bộ đọc API dùng endpoint hiện có. Artifact được tải cục bộ cho chế độ offline và bộ tiền xử lý. Các dịch vụ hiện tại tiếp tục là nguồn quan sát live. Ngưỡng benchmark 0,00672966, MQTT 0,01184 và camera 0,01114.
