# Kiểm định baseline V3.1

Production chưa có phiên validation đủ điều kiện. False incidents/hour, anomaly rate, CPU/RAM overhead và latency inference của baseline thật chưa có số đo.

Code tính tỷ lệ đầu vào chấm được trên toàn bộ cửa sổ đã ghi audit trong phiên, gồm cửa sổ bị loại. Cổng yêu cầu tối thiểu 90% đủ điều kiện, ít nhất 36 mẫu chấm được cho từng phần calibration/validation và không quá 5% cửa sổ validation bị đánh dấu bất thường. Báo cáo đo phân phối điểm hai detector, missingness, tỷ lệ loại, false incidents/hour theo hysteresis, CPU time, RSS và thời gian chấm batch.

Fixture độc lập theo phiên mô phỏng MQTT tải thấp, burst/reconnect hợp lệ và Video streaming/bitrate/playback. Cả hai profile fixture qua validation, 0 incident trong fixture. Đây là bằng chứng logic; chưa có số đo tương ứng trên các kịch bản thật. Chưa có ground truth tấn công độc lập ở topology hiện tại.
