# Video Recording Service

Standalone Python service that reads the existing camera MQTT status topic, takes the current HTTP MJPEG URL, and supervises one FFmpeg process for each configured device. It writes H.264 MP4 segments and indexes completed segments in SQLite. It does not change MQTT topics or ESP32-CAM firmware.

## Windows setup

1. Install FFmpeg and ensure the executable is on `PATH` (or set `FFMPEG_BINARY` to its full path). Prefer a build with `libx264`; NVIDIA NVENC, Intel QSV, and AMD AMF are detected and startup-probed automatically.
2. From the project root, create and activate a virtual environment, then install `recording_service/requirements.txt`.
3. Copy `recording_service/.env.example` to `recording_service/.env` and adjust the broker if needed. Keep `RECORDING_ROOT` on a disk with sufficient free space.
4. Start from the project root with `python -m recording_service.main`.

The recorder waits for retained MQTT status for `ESP32-CAM-01`, so it can start without a manually entered camera address. Files are written under `recordings/<device_id>/<YYYY-MM-DD>/<HH-MM-SS>.mp4`. SQLite metadata is stored at `recording_service/data/recordings.sqlite3` by default.

The quota uses GiB units (`RECORDING_MAX_STORAGE_GB=5` means 5 × 1024³ bytes). FFmpeg splits at wall-clock boundaries. Recently modified MP4 files are protected from quota eviction because the segment muxer has not yet reported them as closed. The service sends `q` to FFmpeg and waits for process exit during shutdown so MP4 trailers are finalized.

## Camera stream client limit

The current ESP32-CAM firmware accepts one stream client at a time. While this recorder is consuming the stream, AppDemo's direct camera connection may not receive video. The recorder backend does not modify firmware or MQTT topics; simultaneous direct clients require a later stream relay or multi-client firmware change.

