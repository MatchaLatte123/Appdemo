import copy
import json
import os
import re
import threading
import time
from uuid import uuid4

import paho.mqtt.client as mqtt
from mcp.server.fastmcp import FastMCP

HOST = os.getenv("MQTT_HOST", "127.0.0.1")
PORT = int(os.getenv("MQTT_PORT", "1883"))
STALE = int(os.getenv("STATUS_MAX_AGE", "90"))
PREFIX = "smarthome/device/"
DEVICES = {"relay01": "ESP32-RELAY-01", "fan01": "ESP32-FAN-01"}
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
RELAY_PIRS = {
    "PIR-SR602-01": 16,
    "PIR-SR602-02": 17,
    "PIR-SR602-03": 25,
    "PIR-SR602-04": 26,
}
RELAY_MODES = ("manual", "motion", "schedule", "schedule_motion")
FAN_MODES = {(False, False): "manual", (True, False): "temperature",
             (False, True): "schedule", (True, True): "both"}
MAX_TIMER_SECONDS = 86400
CONFIG_PACKET_LIMITS = {"relay01": 2048, "fan01": 1024}

cache = {}
updates = threading.Condition()
automation_lock = threading.Lock()
connected = threading.Event()
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="xiaozhi_gateway")
client.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASSWORD"])
mcp = FastMCP("SmartHome MQTT")


def topic(device, suffix):
    return f"{PREFIX}{device}/{suffix}"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        for device in DEVICES:
            for suffix in ("status", "config", "config_ack"):
                client.subscribe(topic(device, suffix), qos=1)
        client.subscribe(topic("fan01", "telemetry"), qos=1)
        connected.set()


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    with updates:
        connected.clear()
        cache.clear()
        updates.notify_all()


def on_message(client, userdata, msg):
    with updates:
        if not msg.payload:
            cache.pop(msg.topic, None)
        else:
            try:
                data = json.loads(msg.payload)
            except (UnicodeError, ValueError):
                return
            if not isinstance(data, dict):
                return
            cache[msg.topic] = (time.monotonic(), msg.retain, data)
        updates.notify_all()


def status(device):
    with updates:
        item = cache.get(topic(device, "status"))
    if not item:
        return None, "Chưa nhận trạng thái thiết bị."
    timestamp, retained, data = item
    if data.get("online") is not True:
        return None, "Thiết bị chưa báo online."
    if retained or time.monotonic() - timestamp > STALE:
        return None, "Cần trạng thái trực tiếp và còn hạn từ thiết bị."
    if data.get("quarantined") or data.get("security_state") == "QUARANTINED":
        return None, "Thiết bị đang bị cách ly bảo mật."
    return data, None


def send(device, suffix, payload, retain=False):
    destination = topic(device, suffix)
    try:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        if suffix == "config" and 9 + len(destination) + len(body) > CONFIG_PACKET_LIMITS[device]:
            return "Profile quá lớn so với bộ đệm MQTT của firmware."
        result = client.publish(
            destination, body, qos=1, retain=retain,
        )
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            result.wait_for_publish(timeout=3)
            if result.is_published():
                return None
    except (RuntimeError, ValueError) as exc:
        return f"Lỗi gửi MQTT: {exc}"
    return "Chưa xác nhận broker nhận lệnh MQTT."


def publish(device, payload):
    if not connected.is_set():
        return "Lỗi: MQTT chưa kết nối."
    _, error = status(device)
    if error:
        return "Lỗi: " + error
    return send(device, "control", payload) or "Đã gửi lệnh MQTT; thiết bị chưa xác nhận thực thi."


def wait_for_message(device, suffix, since, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    with updates:
        while connected.is_set():
            item = cache.get(topic(device, suffix))
            if item:
                timestamp, retained, data = item
                if timestamp >= since and not retained and predicate(data):
                    return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            updates.wait(remaining)
    return None


def update_profile(device, edit):
    with automation_lock:
        with updates:
            if not connected.is_set():
                return "Lỗi: MQTT chưa kết nối."
            current, error = status(device)
            if error:
                return "Lỗi: " + error
            item = cache.get(topic(device, "config"))
            profile = copy.deepcopy(item[2]) if item else {}
            version = profile.get("version")
            if (
                profile.get("device_id") != DEVICES[device]
                or profile.get("reset_profile")
                or type(version) is not int or not 0 <= version < 2147483647
                or version != current.get("profile_version")
                or not isinstance(profile.get("settings"), dict)
            ):
                return "Cần profile MQTT đã áp dụng. Hãy kiểm tra và đồng bộ profile thiết bị từ AppDemo."
            settings = profile["settings"]
            if device == "relay01":
                channels = settings.get("channels")
                if (
                    not isinstance(channels, list) or len(channels) != 4
                    or any(not isinstance(c, dict) or c.get("id") != index
                           for index, c in enumerate(channels, 1))
                ):
                    return "Profile relay phải có đủ bốn kênh theo thứ tự."
            error = edit(settings)
            if error:
                return "Lỗi: " + error
            profile["version"] = version + 1
            since = time.monotonic()
        error = send(device, "config", profile, retain=True)
        if error:
            return error
        ack = wait_for_message(
            device, "config_ack", since,
            lambda data: data.get("device_id") == DEVICES[device]
            and data.get("version") == profile["version"],
        )
        if ack and ack.get("status") == "APPLIED":
            wait_for_message(
                device, "status", since,
                lambda data: data.get("profile_version") == profile["version"],
                timeout=1,
            )
            return f"Thiết bị đã áp dụng cấu hình automation phiên bản {profile['version']}."
        if ack:
            return f"Thiết bị báo trạng thái cấu hình: {ack.get('status')}."
        return "Đã gửi cấu hình; chưa nhận xác nhận thiết bị. Hãy kiểm tra trước khi gửi lại."


def schedule_values(schedule_on, schedule_off, schedule_days):
    for value in (schedule_on, schedule_off):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Giờ phải theo dạng HH:MM (24 giờ).")
    if not schedule_days or any(type(day) is not int or not 1 <= day <= 7 for day in schedule_days):
        raise ValueError("Chọn ngày từ 1 (Chủ nhật) đến 7 (Thứ bảy).")
    return {
        "schedule_on": schedule_on, "schedule_off": schedule_off,
        "schedule_days": [DAYS[day - 1] for day in sorted(set(schedule_days))],
    }


def relay_mode(channel, mode):
    channel["automation_mode"] = mode
    channel["mode"] = "schedule" if mode in ("schedule", "schedule_motion") else "manual"


def configured_relay_pir(channel):
    motion = channel.get("motion")
    return (
        isinstance(motion, dict) and motion.get("configured") is True
        and motion.get("sensor_id") in RELAY_PIRS
        and motion.get("gpio") == RELAY_PIRS[motion["sensor_id"]]
    )


def relay_current_mode(channel):
    return channel.get("automation_mode", "schedule" if channel.get("mode") == "schedule" else "manual")


def fan_mode(settings, temperature=None, schedule=None, default_level=None):
    previous = settings.get("scenario_mode", "temperature" if settings.get("auto_mode") else "manual")
    if temperature is None:
        temperature = previous in ("temperature", "both")
    if schedule is None:
        schedule = previous in ("schedule", "both")
    motion = settings.get("motion") or {}
    level = settings.get("default_level") if default_level is None else default_level
    if default_level is not None or temperature or schedule or motion.get("enabled"):
        if type(level) is not int or not 1 <= level <= 3:
            return "Hãy chọn default_level từ 1 đến 3 để quạt có mức chạy tự động."
        settings["default_level"] = level
    settings.update(
        scenario_mode=FAN_MODES[temperature, schedule],
        auto_mode=temperature or schedule,
        temperature_enabled=temperature, schedule_enabled=schedule,
    )
    return None


@mcp.tool()
def set_relay(channel: int, on: bool) -> str:
    """Bật hoặc tắt thủ công một kênh relay từ 1 đến 4."""
    if not 1 <= channel <= 4:
        return "Lỗi: channel phải từ 1 đến 4."
    return publish("relay01", {"action": "SET_CHANNEL", "channel": channel, "state": on})


@mcp.tool()
def set_fan_level(level: int) -> str:
    """Điều khiển quạt thủ công: 0=tắt, 1=thấp, 2=trung bình, 3=cao."""
    if not 0 <= level <= 3:
        return "Lỗi: level phải từ 0 đến 3."
    return publish("fan01", {"action": "SET_LEVEL", "level": level})


@mcp.tool()
def get_device_status(device: str) -> str:
    """Đọc trạng thái relay01 hoặc fan01, gồm tuổi dữ liệu và trạng thái bảo mật."""
    if device not in DEVICES:
        return "Thiết bị không hợp lệ."
    with updates:
        item = cache.get(topic(device, "status"))
    if not item:
        return "Chưa nhận trạng thái."
    timestamp, retained, data = item
    age = time.monotonic() - timestamp
    return json.dumps(
        {"age_seconds": round(age, 1), "fresh": not retained and age <= STALE, "status": data},
        ensure_ascii=False,
    )


@mcp.tool()
def get_fan_telemetry() -> str:
    """Đọc nhiệt độ, độ ẩm và mức quạt từ telemetry fan01."""
    with updates:
        item = cache.get(topic("fan01", "telemetry"))
    if not item:
        return "Chưa nhận telemetry quạt."
    timestamp, retained, data = item
    if retained or time.monotonic() - timestamp > STALE:
        return "Dữ liệu telemetry đã cũ."
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def set_relay_schedule(channel: int, schedule_on: str, schedule_off: str, schedule_days: list[int]) -> str:
    """Cài lịch HH:MM cho kênh 1–4. Ngày 1=Chủ nhật, 2=Thứ hai, …, 7=Thứ bảy. Nếu PIR đang bật, lịch giới hạn giờ PIR."""
    if not 1 <= channel <= 4:
        return "Lỗi: channel phải từ 1 đến 4."
    try:
        schedule = schedule_values(schedule_on, schedule_off, schedule_days)
    except ValueError as exc:
        return str(exc)

    def edit(settings):
        selected = settings["channels"][channel - 1]
        motion = relay_current_mode(selected) in ("motion", "schedule_motion")
        if motion and not configured_relay_pir(selected):
            return "Hãy cấu hình PIR hợp lệ trước khi dùng lịch kết hợp PIR."
        selected.update(schedule)
        relay_mode(selected, "schedule_motion" if motion else "schedule")

    return update_profile("relay01", edit)


@mcp.tool()
def set_motion(channel: int, hold_seconds: int, sensor_id: str | None = None) -> str:
    """Bật PIR relay, giữ ON 1–3600 giây. Giữ lịch đang bật làm khung giờ PIR. Giữ cảm biến kênh đã chọn hoặc chỉ định PIR-SR602-01/02."""
    if not 1 <= channel <= 4 or not 1 <= hold_seconds <= 3600:
        return "Lỗi: channel phải từ 1 đến 4 và hold_seconds từ 1 đến 3600."

    def edit(settings):
        selected = settings["channels"][channel - 1]
        motion = dict(selected.get("motion") or {})
        sensor = sensor_id if sensor_id is not None else motion.get("sensor_id")
        if sensor not in RELAY_PIRS:
            return "Kênh chưa chọn PIR có dây. Hãy chọn PIR-SR602-01 (GPIO16) hoặc PIR-SR602-02 (GPIO17)."
        motion.update(sensor_id=sensor, gpio=RELAY_PIRS[sensor], configured=True, hold_seconds=hold_seconds)
        selected["motion"] = motion
        mode = relay_current_mode(selected)
        relay_mode(selected, "schedule_motion" if mode in ("schedule", "schedule_motion") else "motion")

    return update_profile("relay01", edit)


@mcp.tool()
def set_relay_automation_mode(channel: int, mode: str) -> str:
    """Chọn manual, motion, schedule hoặc schedule_motion cho kênh relay 1–4. Dùng manual để tắt tự động."""
    if not 1 <= channel <= 4 or mode not in RELAY_MODES:
        return "Kênh hoặc chế độ relay không hợp lệ."

    def edit(settings):
        selected = settings["channels"][channel - 1]
        if mode in ("motion", "schedule_motion") and not configured_relay_pir(selected):
            return "Hãy cấu hình PIR bằng set_motion trước."
        relay_mode(selected, mode)

    return update_profile("relay01", edit)


@mcp.tool()
def set_fan_schedule(
    schedule_on: str | None = None, schedule_off: str | None = None,
    schedule_days: list[int] | None = None,
    default_level: int | None = None, schedule_enabled: bool = True,
) -> str:
    """Cài lịch quạt HH:MM, ngày 1=Chủ nhật đến 7=Thứ bảy. Nếu đang bật nhiệt độ, cần thỏa cả hai điều kiện. Mức chạy 1–3."""
    if schedule_enabled and (schedule_on is None or schedule_off is None or schedule_days is None):
        return "Hãy cung cấp giờ bật, giờ tắt và các ngày khi bật lịch quạt."
    try:
        schedule = schedule_values(schedule_on, schedule_off, schedule_days) if schedule_enabled else {}
    except ValueError as exc:
        return str(exc)

    def edit(settings):
        settings.update(schedule)
        return fan_mode(settings, schedule=schedule_enabled, default_level=default_level)

    return update_profile("fan01", edit)


@mcp.tool()
def set_fan_temperature(
    temperature_enabled: bool, temp_on: float | None = None, temp_off: float | None = None,
    default_level: int | None = None,
) -> str:
    """Bật/tắt tự động theo nhiệt độ. Khi bật, đặt ngưỡng °C với ngưỡng tắt thấp hơn ngưỡng bật. Mức chạy 1–3."""
    if temperature_enabled and (
        temp_on is None or temp_off is None or not 0 <= temp_off < temp_on <= 100
    ):
        return "Lỗi: cần 0 ≤ nhiệt độ tắt < nhiệt độ bật ≤ 100°C."

    def edit(settings):
        if temperature_enabled:
            settings.update(temperature_on=temp_on, temperature_off=temp_off)
        return fan_mode(settings, temperature=temperature_enabled, default_level=default_level)

    return update_profile("fan01", edit)


@mcp.tool()
def set_fan_motion(
    enabled: bool, hold_seconds: int | None = None, default_level: int | None = None,
) -> str:
    """Bật/tắt PIR quạt, Hold 1–3600 giây, mức chạy 1–3. Ở manual, PIR có thể tự bật quạt; ở chế độ tự động, PIR là điều kiện bổ sung."""
    if hold_seconds is not None and not 1 <= hold_seconds <= 3600:
        return "Lỗi: hold_seconds phải từ 1 đến 3600."

    def edit(settings):
        motion = dict(settings.get("motion") or {})
        motion.update(enabled=enabled, gpio=22)
        motion.setdefault("sensor_id", "PIR-FAN-01")
        motion.setdefault("cooldown_seconds", 3)
        motion.setdefault("hold_seconds", 120)
        if hold_seconds is not None:
            motion["hold_seconds"] = hold_seconds
        settings["motion"] = motion
        return fan_mode(settings, default_level=default_level)

    return update_profile("fan01", edit)


@mcp.tool()
def creat_timer_fan(delay_seconds: int) -> str:
    """Hẹn quạt tắt sau 1–86400 giây. Khi hết giờ quạt chuyển sang manual và tắt."""
    if not 1 <= delay_seconds <= MAX_TIMER_SECONDS:
        return "Lỗi: delay_seconds phải từ 1 đến 86400."
    with automation_lock:
        if not connected.is_set():
            return "Lỗi: MQTT chưa kết nối."
        data, error = status("fan01")
        if error:
            return "Lỗi: " + error
        if "off_timer_id" not in data:
            return "Firmware quạt chưa hỗ trợ hẹn giờ MQTT."
        timer_id = uuid4().hex
        since = time.monotonic()
        error = send("fan01", "control", {
            "action": "SET_OFF_TIMER", "delay_seconds": delay_seconds, "timer_id": timer_id,
        })
        if error:
            return error
        confirmation = wait_for_message(
            "fan01", "status", since, lambda data: data.get("off_timer_id") == timer_id,
        )
        if confirmation:
            return f"Quạt đã nhận hẹn giờ tắt sau {delay_seconds} giây."
        return "Đã gửi lệnh hẹn giờ; quạt chưa xác nhận."


if __name__ == "__main__":
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(HOST, PORT, keepalive=30)
    client.loop_start()
    try:
        mcp.run(transport="stdio")
    finally:
        client.disconnect()
        client.loop_stop()
