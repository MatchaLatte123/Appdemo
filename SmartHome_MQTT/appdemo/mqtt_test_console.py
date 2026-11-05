import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import queue
import time
import calendar as pycalendar
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
import cv2
import numpy as np
from PIL import Image, ImageTk
import paho.mqtt.client as mqtt


# ============================================================
# CONFIG
# ============================================================

DEFAULT_BROKER = "192.168.1.130"
DEFAULT_PORT = 1883

T = {
    "rs": "smarthome/device/relay01/status",
    "rt": "smarthome/device/relay01/telemetry",
    "rc": "smarthome/device/relay01/control",

    "fs": "smarthome/device/fan01/status",
    "ft": "smarthome/device/fan01/telemetry",
    "fc": "smarthome/device/fan01/control",

    "cs": "smarthome/device/cam01/status",
    "csec": "smarthome/device/cam01/security",

    "sec": "smarthome/security/#",
}


client = None

# ============================================================
# DEVICE PROFILE / LOCAL CACHE
# ============================================================

PROFILE_FILE = Path(__file__).with_name("device_profiles.json")
DISCOVERY_TOPIC = "smarthome/device/announce"
REGISTER_TOPIC = "smarthome/device/register"
profiles = {}
pending_devices = {}

# A device is considered "nearby" only while live MQTT traffic is received.
# Fan telemetry is sent every 10 seconds and announce/register every 30 seconds,
# so 25 seconds gives enough tolerance without keeping powered-off devices.
DISCOVERY_TTL_SECONDS = 15
DISCOVERY_CLEANUP_INTERVAL_MS = 2000
RECORDING_SERVICE_STATUS_URL = "http://192.168.1.130:8765/api/status"
RECORDING_SERVICE_BASE_URL = "http://192.168.1.130:8765"
recording_service_status = "SERVER OFFLINE"
recording_status_check_lock = threading.Lock()
recording_status_check_running = False
camera_center = None

def load_profiles():
    global profiles
    try:
        if PROFILE_FILE.exists():
            profiles = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        profiles = {}
        print(f"Profile load error: {e}")

def save_profiles():
    try:
        PROFILE_FILE.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log(f"PROFILE SAVE ERROR: {e}")

def device_short_id(device_id):
    s = device_id.lower().replace("esp32-", "")
    return s.replace("_", "").replace("-", "")

def config_topic_for(device_id, device_type=None):
    p = profiles.get(device_id, {})
    if p.get("config_topic"):
        return p["config_topic"]
    short = device_short_id(device_id)
    # Preserve the project's existing naming convention.
    if device_type == "fan" or "fan" in short:
        return f"smarthome/device/{short}/config"
    if device_type == "relay" or "relay" in short:
        return f"smarthome/device/{short}/config"
    if device_type == "camera" or "cam" in short:
        return f"smarthome/device/{short}/config"
    return f"smarthome/device/{short}/config"

def default_settings_for(device_type):
    """Return the application default profile for a device type."""
    dtype = str(device_type or "unknown").lower()
    if dtype == "fan":
        return {
            "default_level": 1,
            "auto_mode": False,
            "scenario_mode": "manual",
            "temperature_enabled": False,
            "temperature_on": 30,
            "temperature_off": 27,
            "schedule_enabled": False,
            "schedule_on": "08:00",
            "schedule_off": "22:00",
            "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "motion": {
                "enabled": False,
                "sensor_id": "PIR-FAN-01",
                "gpio": 22,
                "hold_seconds": 120,
                "cooldown_seconds": 3,
            }
        }
    if dtype == "relay":
        pir_defaults = [
            ("PIR-SR602-01", 16), ("PIR-SR602-02", 17),
            ("PIR-SR602-03", None), ("PIR-SR602-04", None),
        ]
        days_default = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {
            "rooms": [],
            "channels": [
                {"id": i + 1, "name": f"Relay {i + 1}", "room": "", "automation_mode": "manual", "motion": {"sensor_id": sensor, "gpio": gpio, "configured": False, "hold_seconds": 120, "cooldown_seconds": 3}, "default_state": False, "schedule_on": "08:00", "schedule_off": "22:00", "schedule_days": days_default.copy()}
                for i, (sensor, gpio) in enumerate(pir_defaults)
            ]
        }
    if dtype == "camera":
        return {
            "security_state": "NORMAL"
        }
    return {}


def reset_profile_to_default(device_id, device_type=None):
    """Reset the local profile to the default template before removal."""
    p = profiles.get(device_id, {})
    dtype = device_type or p.get("type", "unknown")
    info = pending_devices.get(device_id, {})
    default_name = info.get("name") or p.get("name") or device_id
    default_room = info.get("room") or p.get("room") or ""
    profiles[device_id] = {
        "device_id": device_id,
        "type": dtype,
        "name": default_name,
        "room": default_room,
        "settings": default_settings_for(dtype),
        "config_topic": p.get("config_topic") or info.get("config_topic") or config_topic_for(device_id, dtype),
        "version": 0,
        "last_seen": p.get("last_seen", "")
    }
    return profiles[device_id]


def delete_device(device_id):
    p = profiles.get(device_id)
    if not p:
        return

    dtype = p.get("type", "unknown")
    answer = messagebox.askyesno(
        "Xóa thiết bị",
        f"Xóa thiết bị {p.get('name', device_id)} ({device_id})?\n\n"
        "Trước khi xóa, thiết bị sẽ được đưa về profile mặc định.\n"
        "Sau đó profile tùy chỉnh mới bị xóa khỏi AppDemo."
    )
    if not answer:
        return

    # --------------------------------------------------------
    # 1. RESET PHYSICAL DEVICE PROFILE FIRST
    # --------------------------------------------------------
    # Do NOT use a timestamp as the profile version here.
    # The firmware receives an explicit reset command and restores
    # its own default profile with profileVersion=0.
    #
    # This is important because the device may currently have a
    # very large/old version in NVS. A normal version=0 profile
    # would be rejected by the firmware's version check.
    default_settings = default_settings_for(dtype)
    config_topic = p.get("config_topic") or config_topic_for(device_id, dtype)

    reset_payload = {
        "device_id": device_id,
        "reset_profile": True,
        "version": 0,
        "name": device_id,
        "room": "",
        "settings": default_settings
    }

    log(f"[PROFILE] RESET BEFORE DELETE {device_id} version=0 topic={config_topic}")
    reset_ok = pub_reliable(
        config_topic,
        json.dumps(reset_payload, ensure_ascii=False),
        retain=True
    )

    if not reset_ok:
        messagebox.showerror(
            "Xóa thiết bị",
            "Không thể gửi lệnh reset profile tới thiết bị.\n"
            "Profile cũ chưa bị xóa khỏi AppDemo."
        )
        return

    # Camera security state has a dedicated security topic in the project.
    # Put it back into NORMAL as part of the delete/reset operation.
    if dtype == "camera":
        security_topic = p.get("security_topic") or f"smarthome/device/{device_short_id(device_id)}/security"
        security_payload = {
            "device_id": device_id,
            "security_state": "NORMAL",
            "quarantined": False
        }
        pub(security_topic, json.dumps(security_payload, ensure_ascii=False), retain=True)

    # Do not add the device to nearby_devices here.
    # A device belongs in the nearby list only when a LIVE MQTT packet
    # has actually been received from it. This prevents a deleted/offline
    # device from reappearing merely because its local profile was removed.
    pending_devices.pop(device_id, None)

    # --------------------------------------------------------
    # 2. REMOVE THE CUSTOM APP PROFILE
    # --------------------------------------------------------
    profiles.pop(device_id, None)
    save_profiles()
    refresh_device_list()
    refresh_device_cards()

    log(f"[PROFILE] DELETE {device_id} -> device reset to default and local profile removed")
    messagebox.showinfo(
        "Đã xóa thiết bị",
        f"Đã xóa {device_id}.\n\n"
        "Profile mặc định đã được gửi trước khi xóa.\n"
        "Khi thêm lại, AppDemo sẽ tạo một profile mới từ cấu hình mặc định."
    )


def save_device_profile(device_id, name=None, room=None, settings=None, device_type=None, config_topic=None):
    old = profiles.get(device_id, {})
    profiles[device_id] = {
        "device_id": device_id,
        "type": device_type or old.get("type", "unknown"),
        "name": name if name is not None else old.get("name", device_id),
        "room": room if room is not None else old.get("room", ""),
        "settings": settings if settings is not None else old.get("settings", {}),
        "config_topic": config_topic or old.get("config_topic") or config_topic_for(device_id, device_type),
        "version": int(old.get("version", 0)),
        "last_seen": old.get("last_seen", "")
    }
    save_profiles()
    refresh_device_list()
    refresh_device_cards(force=True)

def sync_profile(device_id):
    p = profiles.get(device_id)
    if not p:
        messagebox.showwarning("Profile", "Chưa có profile cho thiết bị này.")
        return

    # Device reports the NVS profile version in status/register.
    # Always publish a version newer than both local and device versions.
    local_version = int(p.get("version", 0))
    device_version = int(p.get("device_version", 0))
    p["version"] = max(local_version, device_version) + 1

    settings = dict(p.get("settings", {}))

    # scenario_mode is authoritative for the firmware scenario engine.
    if p.get("type") == "fan":
        mode = settings.get("scenario_mode")
        if mode not in ("manual", "temperature", "schedule", "both"):
            mode = "temperature" if settings.get("auto_mode", False) else "manual"
        settings["scenario_mode"] = mode
        settings["temperature_enabled"] = mode in ("temperature", "both")
        settings["schedule_enabled"] = mode in ("schedule", "both")
        motion_defaults = default_settings_for("fan")["motion"]
        motion = dict(motion_defaults)
        if isinstance(settings.get("motion"), dict):
            motion.update(settings["motion"])
        motion["enabled"] = bool(motion.get("enabled", False))
        motion["sensor_id"] = str(motion.get("sensor_id") or "PIR-FAN-01")
        # This SR602 is physically wired to GPIO22; do not silently remap it.
        motion["gpio"] = 22
        motion["hold_seconds"] = max(1, min(3600, int(motion.get("hold_seconds", 120))))
        motion["cooldown_seconds"] = max(0, min(60, int(motion.get("cooldown_seconds", 3))))
        settings["motion"] = motion
    elif p.get("type") == "relay":
        # Keep four-channel relay profiles backward-compatible with older
        # two-channel local profiles.
        defaults = default_settings_for("relay")
        channels = settings.get("channels")
        if not isinstance(channels, list):
            channels = []
        normalized = []
        for i in range(4):
            base = dict(defaults["channels"][i])
            if i < len(channels) and isinstance(channels[i], dict):
                base.update(channels[i])
            base["id"] = i + 1
            base["name"] = str(base.get("name") or f"Relay {i + 1}")
            base["room"] = str(base.get("room") or "")
            old_mode = base.get("mode", "manual")
            mode = base.get("automation_mode")
            new_profile_mode = mode in ("manual", "motion", "schedule", "schedule_motion")
            if mode not in ("manual", "motion", "schedule", "schedule_motion"):
                mode = "schedule" if old_mode == "schedule" else "manual"
            base["automation_mode"] = mode
            # Older relay firmware reads mode; motion-only safely degrades to manual.
            base["mode"] = "schedule" if mode in ("schedule", "schedule_motion") else "manual"
            defaults_motion = defaults["channels"][i]["motion"]
            motion = dict(defaults_motion)
            if isinstance(base.get("motion"), dict):
                motion.update(base["motion"])
            sensor_gpios = {"PIR-SR602-01": 16, "PIR-SR602-02": 17, "PIR-SR602-03": None, "PIR-SR602-04": None}
            if motion.get("sensor_id") not in sensor_gpios:
                motion["sensor_id"] = defaults_motion["sensor_id"]
            motion["gpio"] = sensor_gpios[motion["sensor_id"]]
            motion["configured"] = bool(motion.get("configured", False)) if new_profile_mode else False
            motion["configured"] = motion["configured"] and motion["gpio"] is not None
            motion.pop("enabled", None)
            motion["hold_seconds"] = max(1, min(3600, int(motion.get("hold_seconds", 120))))
            motion["cooldown_seconds"] = max(0, min(60, int(motion.get("cooldown_seconds", 3))))
            base["motion"] = motion
            base["default_state"] = bool(base.get("default_state", False))
            base["schedule_on"] = str(base.get("schedule_on") or "08:00")
            base["schedule_off"] = str(base.get("schedule_off") or "22:00")
            days = base.get("schedule_days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
            base["schedule_days"] = [d for d in days if d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")] if isinstance(days, list) else ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            normalized.append(base)
        rooms = settings.get("rooms", [])
        if not isinstance(rooms, list):
            rooms = []
        settings["rooms"] = [str(r).strip() for r in rooms if str(r).strip()]
        settings["channels"] = normalized

    topic = p.get("config_topic") or config_topic_for(device_id, p.get("type"))
    p["config_topic"] = topic

    payload = {
        "device_id": device_id,
        "version": p["version"],
        "name": p.get("name", device_id),
        "room": p.get("room", ""),
        "settings": settings
    }

    p["settings"] = settings
    save_profiles()

    log(f"[PROFILE] SYNC {device_id} version={p['version']} topic={topic}")
    pub(topic, json.dumps(payload, ensure_ascii=False), retain=True)

def open_add_device_dialog():
    """Open the Add Device workflow: choose type -> discover nearby -> add."""
    dialog = tk.Toplevel(root)
    dialog.title("Thêm thiết bị")
    dialog.geometry("560x430")
    dialog.transient(root)
    dialog.grab_set()

    ttk.Label(dialog, text="1. Chọn loại thiết bị", font=("Segoe UI", 10, "bold")).pack(
        anchor="w", padx=15, pady=(15, 5)
    )

    type_var = tk.StringVar(value="fan")
    type_frame = ttk.Frame(dialog)
    type_frame.pack(fill="x", padx=15)

    type_combo = ttk.Combobox(
        type_frame,
        textvariable=type_var,
        state="readonly",
        values=("relay", "fan", "camera"),
        width=18
    )
    type_combo.pack(side="left")

    ttk.Label(
        dialog,
        text="Thiết bị gần đây được xác định từ các gói MQTT announce/register nhận được từ cùng broker.",
        foreground="gray",
        wraplength=520
    ).pack(anchor="w", padx=15, pady=(8, 8))

    ttk.Label(dialog, text="2. Thiết bị đang ở gần", font=("Segoe UI", 10, "bold")).pack(
        anchor="w", padx=15, pady=(5, 5)
    )

    list_frame = ttk.Frame(dialog)
    list_frame.pack(fill="both", expand=True, padx=15)

    nearby_list = tk.Listbox(list_frame, height=10, exportselection=False)
    nearby_list.pack(side="left", fill="both", expand=True)

    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=nearby_list.yview)
    scrollbar.pack(side="right", fill="y")
    nearby_list.configure(yscrollcommand=scrollbar.set)

    nearby_ids = []

    def refresh_nearby(*_):
        nearby_list.delete(0, "end")
        nearby_ids.clear()
        selected_type = type_var.get()
        for did, info in sorted(pending_devices.items()):
            dtype = str(info.get("type", "unknown")).lower()
            if dtype == selected_type:
                nearby_ids.append(did)
                name = info.get("name") or did
                ip = info.get("ip") or "IP chưa biết"
                nearby_list.insert("end", f"{name} | {did} | {ip}")

        if not nearby_ids:
            nearby_list.insert("end", "Không tìm thấy thiết bị. Hãy chờ thiết bị announce rồi bấm QUÉT LẠI.")

    def add_selected():
        selection = nearby_list.curselection()
        if not selection or not nearby_ids:
            messagebox.showwarning("Thêm thiết bị", "Chưa chọn thiết bị hợp lệ.")
            return
        did = nearby_ids[selection[0]]
        dialog.destroy()
        open_device_setup_dialog(did)

    type_combo.bind("<<ComboboxSelected>>", refresh_nearby)

    ttk.Button(type_frame, text="QUÉT LẠI", command=refresh_nearby).pack(side="left", padx=8)
    ttk.Button(dialog, text="THÊM THIẾT BỊ ĐÃ CHỌN", command=add_selected).pack(pady=10)
    ttk.Button(dialog, text="Hủy", command=dialog.destroy).pack(pady=(0, 12))

    refresh_nearby()


def open_device_setup_dialog(device_id):
    info = pending_devices.get(device_id, {})
    if not info:
        messagebox.showwarning("Thêm thiết bị", "Thiết bị không còn trong danh sách phát hiện.")
        return

    default_name = info.get("name") or device_id
    default_room = info.get("room") or ""
    dtype = str(info.get("type", "unknown")).lower()

    dialog = tk.Toplevel(root)
    dialog.title("Thiết lập thiết bị")
    dialog.geometry("460x330")
    dialog.transient(root)
    dialog.grab_set()

    ttk.Label(dialog, text=f"Device ID: {device_id}", font=("Segoe UI", 10, "bold")).pack(
        anchor="w", padx=15, pady=(15, 8)
    )
    ttk.Label(dialog, text=f"Loại: {dtype}").pack(anchor="w", padx=15, pady=3)

    ttk.Label(dialog, text="Tên thiết bị").pack(anchor="w", padx=15, pady=(10, 0))
    name_var = tk.StringVar(value=default_name)
    ttk.Entry(dialog, textvariable=name_var, width=48).pack(padx=15, pady=5)

    ttk.Label(dialog, text="Phòng").pack(anchor="w", padx=15, pady=(5, 0))
    room_var = tk.StringVar(value=default_room)
    ttk.Entry(dialog, textvariable=room_var, width=48).pack(padx=15, pady=5)

    def do_add():
        settings = default_settings_for(dtype)

        save_device_profile(
            device_id,
            name_var.get().strip() or default_name,
            room_var.get().strip(),
            settings,
            dtype,
            info.get("config_topic")
        )

        # Copy camera endpoint only after the user confirms adding the device.
        if dtype == "camera":
            p = profiles.get(device_id, {})
            if info.get("ip"):
                p["ip"] = str(info.get("ip")).strip()
            if info.get("stream"):
                p["stream_url"] = str(info.get("stream")).strip()
            if info.get("status_url"):
                p["status_url"] = str(info.get("status_url")).strip()
            if info.get("stream_port") is not None:
                p["stream_port"] = int(info.get("stream_port"))
            if info.get("http_port") is not None:
                p["http_port"] = int(info.get("http_port"))
            save_profiles()

        sync_profile(device_id)
        pending_devices.pop(device_id, None)
        refresh_device_list()
        refresh_camera_stream_panel()
        dialog.destroy()

        if dtype == "camera":
            root.after(150, lambda did=device_id: auto_start_camera_stream(did))

    ttk.Button(dialog, text="THÊM & ĐỒNG BỘ", command=do_add).pack(pady=15)
    ttk.Button(dialog, text="Hủy", command=dialog.destroy).pack()

def edit_profile(device_id):
    p = profiles.get(device_id, {})
    if not p:
        return

    dtype = p.get("type", "unknown")
    dialog = tk.Toplevel(root)
    dialog.title("Device Profile")
    dialog.geometry("1450x720" if dtype == "relay" else ("560x850" if dtype == "fan" else "560x620"))
    dialog.transient(root)
    dialog.grab_set()

    ttk.Label(dialog, text=f"Device ID: {device_id}").pack(anchor="w", padx=15, pady=10)

    ttk.Label(dialog, text="Tên thiết bị").pack(anchor="w", padx=15)
    name = tk.StringVar(value=p.get("name", device_id))
    ttk.Entry(dialog, textvariable=name, width=60).pack(padx=15, pady=5)

    if dtype == "relay":
        settings = dict(p.get("settings", {}))
        defaults = default_settings_for("relay")
        channels = []
        old_channels = settings.get("channels", [])
        for i in range(4):
            c = dict(defaults["channels"][i])
            if i < len(old_channels) and isinstance(old_channels[i], dict):
                c.update(old_channels[i])
            c["id"] = i + 1
            channels.append(c)

        rooms = [str(r).strip() for r in settings.get("rooms", []) if str(r).strip()]
        for c in channels:
            r = str(c.get("room", "")).strip()
            if r and r not in rooms:
                rooms.append(r)

        room_frame = ttk.LabelFrame(dialog, text="Phòng", padding=8)
        room_frame.pack(fill="x", padx=15, pady=8)
        ttk.Label(room_frame, text="Tạo phòng, sau đó có thể gán cùng phòng cho một hoặc nhiều channel.").pack(anchor="w")
        room_var = tk.StringVar()
        room_entry = ttk.Entry(room_frame, textvariable=room_var, width=30)
        room_entry.pack(side="left", padx=(0, 6), pady=5)
        room_combo = ttk.Combobox(room_frame, state="readonly", values=rooms, width=25)
        room_combo.pack(side="left", padx=6)
        room_combo.set(rooms[0] if rooms else "")

        channel_frame = ttk.LabelFrame(dialog, text="4 Channel Relay", padding=8)
        channel_frame.pack(fill="both", expand=True, padx=15, pady=8)

        headers = ["CH", "Tên channel", "Phòng", "Automation", "Mặc định", "PIR", "GPIO", "Hold s", "Cooldown s", "ON", "OFF", "Ngày chạy"]
        for col, text in enumerate(headers):
            ttk.Label(channel_frame, text=text, font=("Segoe UI", 9, "bold")).grid(row=0, column=col, padx=4, pady=4, sticky="w")

        row_vars = []
        for i, c in enumerate(channels, start=1):
            ttk.Label(channel_frame, text=f"CH{i}").grid(row=i, column=0, padx=4, pady=5, sticky="w")
            nvar = tk.StringVar(value=str(c.get("name") or f"Relay {i}"))
            rvar = tk.StringVar(value=str(c.get("room") or ""))
            old_mode = c.get("mode", "manual")
            mvar = tk.StringVar(value=c.get("automation_mode") if c.get("automation_mode") in ("manual", "motion", "schedule", "schedule_motion") else ("schedule" if old_mode == "schedule" else "manual"))
            motion = c.get("motion") if isinstance(c.get("motion"), dict) else {}
            defaults_motion = defaults["channels"][i - 1]["motion"]
            pir_sensor_gpios = {"PIR-SR602-01": 16, "PIR-SR602-02": 17, "PIR-SR602-03": None, "PIR-SR602-04": None}
            sensor_ids = tuple(pir_sensor_gpios)
            initial_sensor = motion.get("sensor_id") if motion.get("sensor_id") in pir_sensor_gpios else defaults_motion["sensor_id"]
            motion_selected = tk.BooleanVar(value=bool(motion.get("configured", False)) and pir_sensor_gpios[initial_sensor] is not None)
            sensor_var = tk.StringVar(value=initial_sensor)
            initial_gpio = pir_sensor_gpios[initial_sensor]
            gpio_var = tk.StringVar(value=f"GPIO{initial_gpio}" if initial_gpio is not None else "Chưa kết nối")
            hold_var = tk.StringVar(value=str(motion.get("hold_seconds", 120)))
            cooldown_var = tk.StringVar(value=str(motion.get("cooldown_seconds", 3)))
            svar = tk.BooleanVar(value=bool(c.get("default_state", False)))
            onvar = tk.StringVar(value=str(c.get("schedule_on") or "08:00"))
            offvar = tk.StringVar(value=str(c.get("schedule_off") or "22:00"))
            day_vars = {day: tk.BooleanVar(value=day in c.get("schedule_days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])) for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
            ttk.Entry(channel_frame, textvariable=nvar, width=18).grid(row=i, column=1, padx=4, pady=5)
            combo = ttk.Combobox(channel_frame, textvariable=rvar, state="readonly", values=rooms, width=18)
            combo.grid(row=i, column=2, padx=4, pady=5)
            if rvar.get():
                combo.set(rvar.get())
            mode_combo = ttk.Combobox(channel_frame, textvariable=mvar, state="readonly", values=("manual", "motion", "schedule", "schedule_motion"), width=17)
            mode_combo.grid(row=i, column=3, padx=4, pady=5)
            ttk.Checkbutton(channel_frame, variable=svar).grid(row=i, column=4, padx=4, pady=5)
            sensor_combo = ttk.Combobox(channel_frame, textvariable=sensor_var, state="readonly", values=sensor_ids, width=17)
            sensor_combo.grid(row=i, column=5, padx=4, pady=5)
            ttk.Label(channel_frame, textvariable=gpio_var).grid(row=i, column=6, padx=4, pady=5)
            hold_entry = ttk.Entry(channel_frame, textvariable=hold_var, width=7)
            hold_entry.grid(row=i, column=7, padx=4, pady=5)
            cooldown_entry = ttk.Entry(channel_frame, textvariable=cooldown_var, width=8)
            cooldown_entry.grid(row=i, column=8, padx=4, pady=5)
            on_entry = ttk.Entry(channel_frame, textvariable=onvar, width=8)
            on_entry.grid(row=i, column=9, padx=4, pady=5)
            off_entry = ttk.Entry(channel_frame, textvariable=offvar, width=8)
            off_entry.grid(row=i, column=10, padx=4, pady=5)
            days_frame = ttk.Frame(channel_frame)
            days_frame.grid(row=i, column=11, padx=2, pady=2, sticky="w")
            for j, (day, label) in enumerate(zip(day_vars, ("T2", "T3", "T4", "T5", "T6", "T7", "CN"))):
                ttk.Checkbutton(days_frame, text=label, variable=day_vars[day]).grid(row=0, column=j, padx=1)
            schedule_widgets = [on_entry, off_entry, days_frame]
            motion_widgets = [channel_frame.grid_slaves(row=i, column=5)[0], channel_frame.grid_slaves(row=i, column=6)[0], hold_entry, cooldown_entry]
            def update_mode_widgets(*_, mv=mvar, sched=schedule_widgets, motion_controls=motion_widgets, channel=i):
                schedule_visible = mv.get() in ("schedule", "schedule_motion")
                motion_visible = mv.get() in ("motion", "schedule_motion")
                for widget in sched:
                    widget.grid() if schedule_visible else widget.grid_remove()
                for widget in motion_controls:
                    widget.grid() if motion_visible else widget.grid_remove()
            mode_combo.bind("<<ComboboxSelected>>", update_mode_widgets)
            mode_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, mv=mvar, selected=motion_selected: selected.set(True)
                if mv.get() in ("motion", "schedule_motion") else None,
                add="+"
            )
            def on_sensor_selected(_event=None, sv=sensor_var, gv=gpio_var, selected=motion_selected, gpio_map=pir_sensor_gpios):
                selected_gpio = gpio_map.get(sv.get())
                gv.set(f"GPIO{selected_gpio}" if selected_gpio is not None else "Chưa kết nối")
                if selected_gpio is not None:
                    selected.set(True)
            sensor_combo.bind("<<ComboboxSelected>>", on_sensor_selected)
            update_mode_widgets()
            row_vars.append((nvar, rvar, mvar, svar, onvar, offvar, day_vars, hold_var, cooldown_var, motion_selected, sensor_var))

        def add_room():
            room = room_var.get().strip()
            if not room:
                messagebox.showwarning("Phòng", "Nhập tên phòng.")
                return
            if room not in rooms:
                rooms.append(room)
            room_combo["values"] = rooms
            for child in channel_frame.winfo_children():
                if isinstance(child, ttk.Combobox):
                    child["values"] = rooms
            room_var.set("")
            room_combo.set(room)

        def remove_room():
            room = room_combo.get().strip()
            if not room:
                return
            if room in rooms:
                rooms.remove(room)
            room_combo["values"] = rooms
            room_combo.set(rooms[0] if rooms else "")
            for child in channel_frame.winfo_children():
                if isinstance(child, ttk.Combobox):
                    child["values"] = rooms
            for _, rvar, *_ in row_vars:
                if rvar.get() == room:
                    rvar.set("")

        ttk.Button(room_frame, text="TẠO PHÒNG", command=add_room).pack(side="left", padx=4)
        ttk.Button(room_frame, text="XÓA PHÒNG", command=remove_room).pack(side="left", padx=4)

        ttk.Label(
            dialog,
            text="Manual: chỉ điều khiển tay | Motion: PIR giữ relay bật theo Hold | Schedule: relay ON theo lịch | Schedule + Motion: lịch chỉ giới hạn giờ PIR hoạt động; relay chỉ ON khi có chuyển động. CH3/CH4 chưa có cảm biến.",
            foreground="gray",
            wraplength=720
        ).pack(anchor="w", padx=15, pady=5)

        def save_edit():
            new_channels = []
            for i, vars_ in enumerate(row_vars, start=1):
                nvar, rvar, mvar, svar, onvar, offvar, day_vars, hold_var, cooldown_var, motion_selected, sensor_var = vars_
                mode_value = mvar.get().strip()
                if mode_value not in ("manual", "motion", "schedule", "schedule_motion"):
                    mode_value = "manual"
                selected_sensor = sensor_var.get()
                selected_gpio = pir_sensor_gpios.get(selected_sensor)
                if mode_value == "motion" and selected_gpio is None:
                    messagebox.showwarning("Automation", "Cảm biến đã chọn chưa có GPIO khả dụng.")
                    return
                new_channels.append({
                    "id": i,
                    "name": nvar.get().strip() or f"Relay {i}",
                    "room": rvar.get().strip(),
                    "automation_mode": mode_value,
                    "motion": {"sensor_id": selected_sensor, "gpio": selected_gpio, "configured": selected_gpio is not None and motion_selected.get(), "hold_seconds": max(1, min(3600, int(hold_var.get() or 120))), "cooldown_seconds": max(0, min(60, int(cooldown_var.get() or 3)))},
                    "default_state": bool(svar.get()),
                    "schedule_on": onvar.get().strip() or "08:00",
                    "schedule_off": offvar.get().strip() or "22:00",
                    "schedule_days": [day for day, var in day_vars.items() if var.get()]
                })

            new_settings = {
                "rooms": rooms,
                "channels": new_channels
            }
            save_device_profile(
                device_id,
                name.get().strip() or device_id,
                p.get("room", ""),
                new_settings,
                "relay",
                p.get("config_topic")
            )
            sync_profile(device_id)
            dialog.destroy()

        ttk.Button(dialog, text="LƯU & ĐỒNG BỘ", command=save_edit).pack(pady=10)
        ttk.Button(dialog, text="Hủy", command=dialog.destroy).pack()
        return

    ttk.Label(dialog, text="Phòng").pack(anchor="w", padx=15)
    room = tk.StringVar(value=p.get("room", ""))
    ttk.Entry(dialog, textvariable=room, width=52).pack(padx=15, pady=5)

    s = dict(p.get("settings", {}))

    if dtype == "fan":
        f = ttk.LabelFrame(dialog, text="Fan scenario", padding=10)
        f.pack(fill="x", padx=15, pady=10)

        mode0 = s.get("scenario_mode")
        if mode0 not in ("manual", "temperature", "schedule", "both"):
            mode0 = "temperature" if s.get("auto_mode", False) else "manual"

        mode = tk.StringVar(value=mode0)
        level = tk.IntVar(value=int(s.get("default_level", 0)))
        ton = tk.DoubleVar(value=float(s.get("temperature_on", 30)))
        toff = tk.DoubleVar(value=float(s.get("temperature_off", 27)))
        schedule_on = tk.StringVar(value=s.get("schedule_on", "08:00"))
        schedule_off = tk.StringVar(value=s.get("schedule_off", "22:00"))
        all_days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        day_labels = ("Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN")
        saved_days = s.get("schedule_days", list(all_days))
        schedule_day_vars = {day: tk.BooleanVar(value=day in saved_days) for day in all_days}
        motion = dict(default_settings_for("fan")["motion"])
        if isinstance(s.get("motion"), dict):
            motion.update(s["motion"])
        motion_enabled = tk.BooleanVar(value=bool(motion.get("enabled", False)))
        motion_sensor_id = tk.StringVar(value=str(motion.get("sensor_id") or "PIR-FAN-01"))
        motion_gpio = tk.IntVar(value=22)
        motion_hold = tk.IntVar(value=int(motion.get("hold_seconds", 120)))
        motion_cooldown = tk.IntVar(value=int(motion.get("cooldown_seconds", 3)))

        ttk.Label(f, text="Scenario mode").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=mode, state="readonly", width=18, values=("manual", "temperature", "schedule", "both")).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Default level (1-3)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(f, from_=0, to=3, textvariable=level, width=8).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Temp ON (°C)").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=ton, width=10).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Temp OFF (°C)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=toff, width=10).grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Schedule ON").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=schedule_on, width=10).grid(row=4, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Schedule OFF").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=schedule_off, width=10).grid(row=5, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Ngày chạy lịch").grid(row=6, column=0, sticky="nw", pady=4)
        days_frame = ttk.Frame(f)
        days_frame.grid(row=6, column=1, sticky="w", padx=8)
        for i, (day, label) in enumerate(zip(all_days, day_labels)):
            ttk.Checkbutton(days_frame, text=label, variable=schedule_day_vars[day]).grid(row=i // 4, column=i % 4, sticky="w")
        ttk.Label(f, text="temperature/both: dùng DHT11 | schedule/both: dùng giờ NTP", foreground="gray").grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 2))

        mf = ttk.LabelFrame(dialog, text="Motion Detection", padding=10)
        mf.pack(fill="x", padx=15, pady=(0, 10))
        ttk.Checkbutton(mf, text="Enable Motion Detection", variable=motion_enabled).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(mf, text="Sensor ID").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(mf, textvariable=motion_sensor_id, width=22).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(mf, text="GPIO").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(mf, textvariable=motion_gpio, width=8, state="readonly").grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(mf, text="Hold Time (seconds)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(mf, from_=1, to=3600, textvariable=motion_hold, width=8).grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(mf, text="Cooldown (seconds)").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Spinbox(mf, from_=0, to=60, textvariable=motion_cooldown, width=8).grid(row=4, column=1, sticky="w", padx=8)

    def save_edit():
        settings = s.copy()
        if dtype == "fan":
            selected_mode = mode.get().strip()
            selected_level = int(level.get())
            if selected_mode != "manual" and selected_level == 0:
                messagebox.showwarning("Profile", "Scenario tự động cần Default level từ 1 đến 3.")
                return
            if selected_mode not in ("manual", "temperature", "schedule", "both"):
                messagebox.showwarning("Profile", "Scenario mode không hợp lệ.")
                return
            settings.update({
                "default_level": selected_level,
                "scenario_mode": selected_mode,
                "auto_mode": selected_mode != "manual",
                "temperature_enabled": selected_mode in ("temperature", "both"),
                "temperature_on": float(ton.get()),
                "temperature_off": float(toff.get()),
                "schedule_enabled": selected_mode in ("schedule", "both"),
                "schedule_on": schedule_on.get().strip(),
                "schedule_off": schedule_off.get().strip(),
                "schedule_days": [day for day in all_days if schedule_day_vars[day].get()],
                "motion": {
                    "enabled": bool(motion_enabled.get()),
                    "sensor_id": motion_sensor_id.get().strip() or "PIR-FAN-01",
                    "gpio": 22,
                    "hold_seconds": max(1, min(3600, int(motion_hold.get()))),
                    "cooldown_seconds": max(0, min(60, int(motion_cooldown.get()))),
                },
            })
        save_device_profile(device_id, name.get().strip() or device_id, room.get().strip(), settings, dtype, p.get("config_topic"))
        sync_profile(device_id)
        dialog.destroy()

    ttk.Button(dialog, text="LƯU & ĐỒNG BỘ", command=save_edit).pack(pady=12)
    ttk.Button(dialog, text="Hủy", command=dialog.destroy).pack()

def sync_selected_profile():
    sel=device_tree.selection()
    if sel: sync_profile(device_tree.item(sel[0],"values")[0])

def edit_selected_profile():
    if "device_tree" not in globals():
        return
    sel = device_tree.selection()
    if sel:
        edit_profile(device_tree.item(sel[0], "values")[0])

def cleanup_stale_pending_devices_once():
    """Synchronously remove stale nearby entries without scheduling another timer."""
    now = time.monotonic()
    stale = []
    for did, info in list(pending_devices.items()):
        last_seen = info.get("last_seen_ts")
        if last_seen is None or now - float(last_seen) > DISCOVERY_TTL_SECONDS:
            stale.append(did)
    for did in stale:
        pending_devices.pop(did, None)
        log(f"[DISCOVERY] Xóa thiết bị stale khỏi nearby: {did}")


def refresh_device_list():
    # Remove stale nearby entries immediately when the user presses REFRESH.
    cleanup_stale_pending_devices_once()
    if "device_tree" in globals():
        for item in device_tree.get_children():
            device_tree.delete(item)
        for did, p in sorted(profiles.items()):
            device_tree.insert("", "end", values=(did, p.get("type", ""), p.get("name", ""), p.get("room", ""), p.get("version", 0)))

    if "pending_list" in globals():
        pending_list.delete(0, "end")
        for did, info in sorted(pending_devices.items()):
            pending_list.insert("end", f"{did} | {info.get('type', 'unknown')} | {info.get('ip', 'IP?')}")

    # MQTT discovery/status messages call this often. Rebuild the device cards
    # only when the actual device set changes, so edits and the scroll position
    # survive routine list refreshes.
    if set(device_widgets) != set(profiles):
        refresh_device_cards()


def add_pending_selected():
    # Backward-compatible helper; the normal flow now starts from ADD DEVICE.
    open_add_device_dialog()

load_profiles()

# ============================================================
# GUI HELPERS
# ============================================================

def log(text):
    root.after(
        0,
        lambda: (
            logbox.insert("end", text + "\n"),
            logbox.see("end")
        )
    )


def set_label(widget, text):
    root.after(
        0,
        lambda: widget.config(text=text)
    )


# ============================================================
# MQTT CALLBACKS - PAHO MQTT 2.x
# ============================================================

def connected(c, userdata, flags, reason_code, properties=None):

    # Paho MQTT 2.x:
    # reason_code is a ReasonCode object
    if reason_code.is_failure:
        log(f"MQTT CONNECT FAILED: {reason_code}")
        set_label(conn, "ERROR")
        return

    log(f"MQTT CONNECTED: {reason_code}")
    set_label(conn, "ONLINE")

    topics = [
        T["rs"],
        T["rt"],
        T["fs"],
        T["ft"],
        T["cs"],
        T["csec"],
        T["sec"],
        DISCOVERY_TOPIC,
        REGISTER_TOPIC,
        "smarthome/device/+/config_ack",
        "smarthome/device/#",
    ]

    for topic in topics:
        result = c.subscribe(topic)

        if result[0] == mqtt.MQTT_ERR_SUCCESS:
            log(f"SUBSCRIBE {topic}")
        else:
            log(f"SUBSCRIBE FAILED {topic}: rc={result[0]}")


def disconnected(c, userdata, disconnect_flags, reason_code, properties=None):
    log(f"MQTT DISCONNECTED: {reason_code}")
    set_label(conn, "OFFLINE")


def update_card(did, key, value):
    widgets = device_widgets.get(did, {}) if "device_widgets" in globals() else {}
    widget = widgets.get(key)
    if widget is not None:
        set_label(widget, value)


def touch_pending_device(device_id, data=None):
    """Mark a discovered device as currently reachable via live MQTT."""
    if device_id in profiles:
        return

    info = pending_devices.get(device_id, {})
    if data:
        info.update(data)
    info["device_id"] = device_id
    info["last_seen_ts"] = time.monotonic()
    pending_devices[device_id] = info


def cleanup_stale_pending_devices():
    """Remove devices that have stopped sending live MQTT discovery/status traffic."""
    now = time.monotonic()
    stale = []

    for did, info in list(pending_devices.items()):
        last_seen = info.get("last_seen_ts")
        if last_seen is None:
            # A pending device without a live timestamp is not considered
            # nearby. It may be a stale entry created by an older AppDemo.
            stale.append(did)
            continue
        if now - float(last_seen) > DISCOVERY_TTL_SECONDS:
            stale.append(did)

    if stale:
        for did in stale:
            pending_devices.pop(did, None)
            log(f"[DISCOVERY] Thiết bị không còn online, xóa khỏi nearby: {did}")
        root.after(0, refresh_device_list)

    root.after(DISCOVERY_CLEANUP_INTERVAL_MS, cleanup_stale_pending_devices)


def get_configured_camera():
    """Return the first confirmed camera profile, or (None, None)."""
    for did, profile in profiles.items():
        if str(profile.get("type", "")).lower() == "camera":
            return did, profile
    return None, None


def refresh_camera_stream_panel():
    """Show recorder status without opening a competing ESP32 stream client."""
    if "streamf" not in globals() or "stream_status" not in globals():
        return
    try:
        if not stream_status.winfo_exists():
            return
    except tk.TclError:
        return
    did, profile = get_configured_camera()
    if profile is None:
        if "camera_ip" in globals():
            camera_ip.set("")
        return
    ip = str(profile.get("ip") or "").strip()
    camera_ip.set(ip)
    stream_status.config(text=recording_service_status)
    camera_view.config(text=f"Recording Service\n{recording_service_status}")


def auto_start_camera_stream(device_id=None):
    """Refresh server status; the AppDemo must not connect to the ESP32 stream."""
    did, profile = get_configured_camera()
    if profile is None or (device_id and did != device_id):
        return
    refresh_camera_stream_panel()
    refresh_recording_service_status()


def refresh_recording_service_status():
    """Poll the local recorder API in the background, never the camera URL."""
    global recording_status_check_running
    if not recording_status_check_lock.acquire(blocking=False):
        return
    recording_status_check_running = True

    def worker():
        global recording_service_status, recording_status_check_running
        try:
            with urlopen(RECORDING_SERVICE_STATUS_URL, timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            camera_id, _profile = get_configured_camera()
            camera = next((item for item in payload.get("cameras", [])
                           if item.get("camera_id") == camera_id), None)
            if camera is None:
                state = "CAMERA NOT CONFIGURED"
            else:
                state = str(camera.get("recorder_state", "UNKNOWN")).upper()
                if camera.get("last_error"):
                    state += " | " + str(camera["last_error"])[-100:]
            recording_service_status = state
        except Exception:
            recording_service_status = "SERVER OFFLINE"
        finally:
            recording_status_check_running = False
            recording_status_check_lock.release()
            try:
                root.after(0, refresh_camera_stream_panel)
            except (NameError, tk.TclError):
                pass

    threading.Thread(target=worker, name="recording-status", daemon=True).start()


def poll_recording_service_status():
    refresh_recording_service_status()
    root.after(5000, poll_recording_service_status)


class CameraCenter:
    """Tkinter camera UI. HTTP and video decoding run outside Tk's main thread."""

    def __init__(self):
        self.window = tk.Toplevel(root)
        self.window.title("Camera Server - Live & Playback")
        self.window.geometry("1040x760")
        self.window.minsize(850, 620)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.base_url = RECORDING_SERVICE_BASE_URL.rstrip("/")
        self.camera_id = tk.StringVar()
        self.service_status = tk.StringVar(value="Đang tải trạng thái Server...")
        self.storage_status = tk.StringVar(value="Storage: --")
        self.current_max_storage_gb = 5.0
        self.current_cleanup_target_gib = 4.5
        self.current_storage_dir = ""
        self.current_cleanup_enabled = True
        self.current_storage_used_bytes = 0
        self.storage_update_busy = False
        self.media_status = tk.StringVar(value="Chưa phát video")
        now = datetime.now().astimezone()
        self.date_var = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        self.date_label_var = tk.StringVar(value=now.strftime("%d/%m/%Y"))
        self.start_hour_var = tk.StringVar(value="14")
        self.start_minute_var = tk.StringVar(value="00")
        self.end_hour_var = tk.StringVar(value="15")
        self.end_minute_var = tk.StringVar(value="00")
        self.date_picker = None
        self.calendar_year = now.year
        self.calendar_month = now.month
        self.speed_var = tk.StringVar(value="1.0x")
        self.camera_states = {}
        self.manifest = None
        self.busy = False
        self.status_busy = False
        self.live_session_id = None
        self.active_capture = None
        self.live_starting = False
        self.live_closing = False
        self.playback_pending = False
        self.cancel_live = threading.Event()
        self.cancel_playback = threading.Event()
        self.playback_export_id = None
        self.playback_segments = []
        self.playback_segment_index = 0
        self.playback_gaps = []
        self.pump_thread = None
        self.pump_stop = threading.Event()
        self.player_lock = threading.Lock()
        self.player_state = {"frame": None, "position": 0.0, "duration": 0.0,
                             "status": "", "paused": False, "seek": None, "speed": 1.0}
        self.timeline_anchor = None
        self.timeline_duration = 0.0
        self.ui_events = queue.SimpleQueue()
        self.updating_scale = False
        self.closed = False
        self.ui_fps_window_started = time.monotonic()
        self.ui_frames_presented = 0
        self.ui_fps = 0.0
        self.video_photo = None

        controls = ttk.Frame(self.window, padding=10)
        controls.pack(fill="x")
        ttk.Label(controls, text="Camera").pack(side="left")
        self.camera_combo = ttk.Combobox(controls, textvariable=self.camera_id,
                                         state="readonly", width=24)
        self.camera_combo.pack(side="left", padx=6)
        self.camera_combo.bind("<<ComboboxSelected>>", lambda _event: self._show_camera_state())
        ttk.Button(controls, text="Refresh Server / Storage", command=self.refresh_status).pack(side="left", padx=5)
        ttk.Button(controls, text="Live", command=self.start_live).pack(side="left", padx=5)
        ttk.Button(controls, text="Đóng Live", command=self.stop_live).pack(side="left", padx=5)
        ttk.Label(controls, textvariable=self.service_status).pack(side="left", padx=10)

        storage_row = ttk.Frame(self.window, padding=(10, 0))
        storage_row.pack(fill="x", anchor="w")
        ttk.Label(storage_row, textvariable=self.storage_status).pack(side="left")
        self.storage_settings_button = ttk.Button(
            storage_row, text="Storage Settings",
            command=self.open_storage_settings
        )
        self.storage_settings_button.pack(side="left", padx=10)

        body = ttk.Panedwindow(self.window, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=8)
        video_panel = ttk.Frame(body)
        footage_panel = ttk.Frame(body, width=300)
        body.add(video_panel, weight=4)
        body.add(footage_panel, weight=1)

        self.video_label = ttk.Label(video_panel, text="Chọn camera rồi nhấn Live hoặc Tải footage",
                                     anchor="center", relief="sunken")
        self.video_label.pack(fill="both", expand=True)
        ttk.Label(video_panel, textvariable=self.media_status).pack(fill="x", pady=5)

        playback_controls = ttk.Frame(video_panel)
        playback_controls.pack(fill="x", pady=4)
        self.play_button = ttk.Button(playback_controls, text="Play khoảng giờ", command=self.start_playback)
        self.play_button.pack(side="left", padx=2)
        self.pause_button = ttk.Button(playback_controls, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=2)
        ttk.Button(playback_controls, text="-10s", command=lambda: self.jump(-10)).pack(side="left", padx=2)
        ttk.Button(playback_controls, text="+10s", command=lambda: self.jump(10)).pack(side="left", padx=2)
        ttk.Button(playback_controls, text="Stop Playback", command=self.stop_playback).pack(side="left", padx=2)
        ttk.Label(playback_controls, text="Tốc độ").pack(side="left", padx=(10, 2))
        self.speed_combo = ttk.Combobox(playback_controls, textvariable=self.speed_var,
                                        values=("0.5x", "1.0x", "1.5x", "2.0x"),
                                        state="readonly", width=7)
        self.speed_combo.pack(side="left")
        self.speed_combo.bind("<<ComboboxSelected>>", self._set_speed)
        self.progress = ttk.Scale(video_panel, from_=0, to=1, orient="horizontal")
        self.progress.pack(fill="x", pady=3)
        self.progress.bind("<ButtonRelease-1>", self.seek_to_slider)
        self.timeline_time = tk.StringVar(value="Thời gian: --:--:--")
        ttk.Label(video_panel, textvariable=self.timeline_time, anchor="center").pack(fill="x")
        self.timeline_marks = tk.Canvas(video_panel, height=25, highlightthickness=0)
        self.timeline_marks.pack(fill="x", pady=(0, 4))
        ttk.Label(video_panel, text="Vùng vàng trên timeline = khoảng mất hình; khi phát sẽ hiện màn hình đen.",
                  anchor="w").pack(fill="x")
        self.timeline_marks.bind("<Configure>", lambda _event: self._draw_timeline_marks())

        interval = ttk.LabelFrame(footage_panel, text="Khoảng phát lại", padding=8)
        interval.pack(fill="x", pady=(0, 8))
        ttk.Label(interval, text="Ngày").grid(row=0, column=0, sticky="w")
        ttk.Button(interval, textvariable=self.date_label_var, command=self._open_date_picker).grid(
            row=1, column=0, sticky="w", pady=3
        )
        ttk.Label(interval, text="Từ").grid(row=2, column=0, sticky="w")
        self.start_time_picker = self._time_picker(interval, self.start_hour_var, self.start_minute_var)
        self.start_time_picker.grid(row=3, column=0, sticky="w", pady=3)
        ttk.Label(interval, text="Đến").grid(row=4, column=0, sticky="w")
        self.end_time_picker = self._time_picker(interval, self.end_hour_var, self.end_minute_var)
        self.end_time_picker.grid(row=5, column=0, sticky="w", pady=3)
        ttk.Button(interval, text="Tải danh sách / timeline", command=self.load_footage).grid(row=6, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(footage_panel, text="Footage theo thời gian; GAP là khoảng mất hình.").pack(anchor="w")
        footage_list_frame = ttk.Frame(footage_panel)
        footage_list_frame.pack(fill="both", expand=True, pady=5)
        self.footage_list = tk.Listbox(footage_list_frame, height=20, width=48)
        footage_yscroll = ttk.Scrollbar(footage_list_frame, orient="vertical", command=self.footage_list.yview)
        footage_xscroll = ttk.Scrollbar(footage_list_frame, orient="horizontal", command=self.footage_list.xview)
        self.footage_list.configure(yscrollcommand=footage_yscroll.set,
                                    xscrollcommand=footage_xscroll.set)
        self.footage_list.grid(row=0, column=0, sticky="nsew")
        footage_yscroll.grid(row=0, column=1, sticky="ns")
        footage_xscroll.grid(row=1, column=0, sticky="ew")
        footage_list_frame.rowconfigure(0, weight=1)
        footage_list_frame.columnconfigure(0, weight=1)
        ttk.Label(footage_panel, text="Khi phát khoảng giờ, Server ghép các segment và biểu diễn gap bằng hình đen.",
                  wraplength=280, foreground="gray").pack(anchor="w")

        self.refresh_status()
        self.window.after(40, self._ui_tick)
        self.window.after(5000, self._periodic_status)

    def _post(self, callback):
        if self.closed:
            return
        self.ui_events.put(callback)

    def _request(self, path, method="GET", payload=None, timeout=15):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(urljoin(self.base_url + "/", path.lstrip("/")), data=data,
                          headers={"Content-Type": "application/json"} if data is not None else {},
                          method=method)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            return json.loads(raw.decode("utf-8"))
        return raw

    def _run_worker(self, work, done, failed=None):
        def worker():
            try:
                value = work()
                self._post(lambda: done(value))
            except Exception as exc:
                self._post(lambda error=str(exc): failed(error) if failed else self._set_status(error))
        threading.Thread(target=worker, name="camera-api", daemon=True).start()

    def _set_status(self, message):
        self.media_status.set(str(message))

    def refresh_status(self):
        if self.status_busy or self.closed:
            return
        self.status_busy = True

        def work():
            return self._request("/api/status", timeout=3)

        def done(data):
            self.status_busy = False
            cameras = data.get("cameras", [])
            ids = [str(c.get("camera_id")) for c in cameras if c.get("camera_id")]
            if not ids:
                ids = [did for did, p in profiles.items() if str(p.get("type", "")).lower() == "camera"]
            current = self.camera_id.get()
            self.camera_combo["values"] = ids
            if current not in ids and ids:
                self.camera_id.set(ids[0])
            self.camera_states = {c.get("camera_id"): c for c in cameras}
            self.service_status.set(f"Server: {data.get('service', 'unknown')}")
            storage = data.get("storage", {})
            used = int(storage.get("used_bytes", 0))
            self.current_storage_used_bytes = used
            maximum = int(storage.get("max_bytes", 0))
            if maximum > 0:
                self.current_max_storage_gb = maximum / (1024 ** 3)
            self.current_cleanup_target_gib = float(storage.get("cleanup_target_gib", 4.5))
            self.current_storage_dir = str(storage.get("storage_dir", self.current_storage_dir))
            self.current_cleanup_enabled = bool(storage.get("cleanup_enabled", True))
            self.storage_status.set(
                f"Storage: {self._bytes(used)} / {self._bytes(maximum)}"
                + (f" | Còn trống: {self._bytes(storage['disk_free_bytes'])}"
                   if storage.get("disk_free_bytes") is not None else " | Storage chưa sẵn sàng")
                + (f" | Cảnh báo: {storage['disk_warning']}" if storage.get("disk_warning") else "")
                + (f" | Dọn dẹp: {storage['warning']}" if storage.get("warning")
                   else (" | Dọn dẹp: bật" if storage.get("cleanup_enabled") else " | Dọn dẹp: tắt"))
            )
            self._show_camera_state()

        def failed(error):
            self.status_busy = False
            self.service_status.set("Server lỗi / offline")
            self.storage_status.set(f"Storage chưa lấy được: {error}")
            self._set_status("Không kết nối được Camera Server")

        self._run_worker(work, done, failed)

    def _periodic_status(self):
        if self.closed:
            return
        self.refresh_status()
        try:
            self.window.after(5000, self._periodic_status)
        except tk.TclError:
            pass

    @staticmethod
    def _bytes(value):
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}"
            size /= 1024

    def open_storage_settings(self):
        if self.storage_update_busy:
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("Dung lượng lưu video")
        dialog.transient(self.window)
        dialog.resizable(False, False)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        path_var = tk.StringVar(value=self.current_storage_dir)
        max_var = tk.StringVar(value=f"{self.current_max_storage_gb:.2f}")
        target_var = tk.StringVar(value=f"{self.current_cleanup_target_gib:.2f}")
        enabled_var = tk.BooleanVar(value=self.current_cleanup_enabled)
        ttk.Label(body, text="Thư mục footage").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=path_var, width=48).grid(row=0, column=1, padx=8, sticky="ew")

        def browse():
            selected = filedialog.askdirectory(parent=dialog, initialdir=path_var.get() or None,
                                               mustexist=False, title="Chọn thư mục lưu footage")
            if selected:
                path_var.set(selected)

        ttk.Button(body, text="Browse...", command=browse).grid(row=0, column=2)
        ttk.Label(body, text="Giới hạn tối đa (GiB)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=max_var, width=14).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(body, text="Ngưỡng sau dọn dẹp (GiB)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=target_var, width=14).grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(body, text="Bật tự động dọn segment cũ", variable=enabled_var).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(body, text="Chỉ segment hoàn tất, cũ nhất và không protected/đang dùng mới đủ điều kiện xóa.\n"
                             "Đổi thư mục không di chuyển footage cũ; recorder sẽ đóng segment rồi chuyển root.",
                  justify="left", wraplength=500).grid(row=4, column=0, columnspan=3,
                                                       sticky="w", pady=(10, 12))
        usage_label = ttk.Label(body, text=f"Đang dùng: {self._bytes(self.current_storage_used_bytes)}  |  "
                                          f"Giới hạn: {self.current_max_storage_gb:.2f} GiB",
                                foreground="gray")
        usage_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 10))
        state_label = ttk.Label(body, text="Recording Service: đang tải trạng thái", foreground="gray")
        state_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 10))

        def populate_from_server(data):
            path_var.set(str(data.get("storage_dir", path_var.get())))
            max_var.set(f"{float(data.get('max_storage_gib', self.current_max_storage_gb)):.2f}")
            target_var.set(f"{float(data.get('cleanup_target_gib', self.current_cleanup_target_gib)):.2f}")
            enabled_var.set(bool(data.get("cleanup_enabled", self.current_cleanup_enabled)))
            free = data.get("disk_free_bytes")
            usage_label.configure(text=f"Đang dùng: {self._bytes(int(data.get('used_bytes') or 0))}  |  "
                                        f"Ổ đĩa còn: {self._bytes(free) if free is not None else 'không khả dụng'}")
            recorders = ", ".join(f"{item.get('camera_id')}: {item.get('recorder_state')}"
                                  for item in data.get("recorders", [])) or "không có recorder"
            warning = data.get("disk_warning") or data.get("warning") or data.get("last_error") or "OK"
            state_label.configure(
                text=f"Recording Service: {data.get('service_state', 'unknown')} | "
                     f"Storage Manager: {'bật' if data.get('cleanup_enabled') else 'tắt'} | "
                     f"{recorders} | {warning}")

        def refresh_settings():
            if self.storage_update_busy:
                return
            self.storage_update_busy = True

            def done(data):
                self.storage_update_busy = False
                populate_from_server(data)

            def failed(error):
                self.storage_update_busy = False
                self._set_status(f"Không tải được Storage Settings: {error}")

            self._run_worker(lambda: self._request("/api/storage/settings", timeout=5), done, failed)

        refresh_settings()

        def save():
            if self.storage_update_busy:
                return
            try:
                new_limit = float(max_var.get().strip().replace(",", "."))
                new_target = float(target_var.get().strip().replace(",", "."))
                if not 0.1 <= new_limit <= 1024 or not 0 <= new_target < new_limit:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Cấu hình không hợp lệ",
                                     "Nhập giới hạn 0.1–1024 GiB và ngưỡng từ 0 đến nhỏ hơn giới hạn.",
                                     parent=dialog)
                return
            current_used = self.current_storage_used_bytes
            confirm_cleanup = False
            same_path = Path(path_var.get().strip()).resolve() == Path(self.current_storage_dir).resolve()
            limit_reduced = new_limit < self.current_max_storage_gb
            cleanup_newly_enabled = enabled_var.get() and not self.current_cleanup_enabled
            if enabled_var.get() and (not same_path or new_limit * (1024 ** 3) < current_used
                                      or limit_reduced or cleanup_newly_enabled):
                if not messagebox.askyesno(
                    "Xác nhận áp dụng Storage Settings",
                    "Khi bật cleanup, Recording Service có thể xóa segment hoàn tất cũ nhất chưa protected/đang dùng "
                    "trong kho được quản lý nếu vượt giới hạn. Đổi thư mục không di chuyển footage cũ. Tiếp tục?",
                    parent=dialog,
                ):
                    return
                confirm_cleanup = True
            dialog.destroy()
            self.storage_update_busy = True
            self._set_status("Đang xác thực và áp dụng Storage Settings trên Server...")

            def work():
                return self._request("/api/storage/settings", "POST",
                                     {"storage_dir": path_var.get().strip(),
                                      "max_storage_gib": new_limit,
                                      "cleanup_target_gib": new_target,
                                      "cleanup_enabled": bool(enabled_var.get()),
                                      "confirm_cleanup": confirm_cleanup}, timeout=60)

            def done(result):
                self.storage_update_busy = False
                self.current_max_storage_gb = float(result.get("max_storage_gib", new_limit))
                self.current_cleanup_target_gib = float(result.get("cleanup_target_gib", new_target))
                self.current_storage_dir = str(result.get("storage_dir", path_var.get()))
                self.current_cleanup_enabled = bool(result.get("cleanup_enabled", enabled_var.get()))
                self._set_status("Đã lưu cấu hình Storage Manager. Footage cũ vẫn nằm ở thư mục trước đó.")
                self.refresh_status()

            def failed(error):
                self.storage_update_busy = False
                self._set_status(f"Không cập nhật được giới hạn dung lượng: {error}")

            self._run_worker(work, done, failed)

        controls = ttk.Frame(body)
        controls.grid(row=7, column=0, columnspan=3, sticky="e")
        ttk.Button(controls, text="Refresh", command=refresh_settings).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Hủy", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(controls, text="Lưu / Áp dụng", command=save).pack(side="right")

    def _show_camera_state(self):
        did = self.camera_id.get()
        camera = self.camera_states.get(did)
        if not camera:
            self.service_status.set("Camera chưa được cấu hình trên Server")
            return
        state = camera.get("camera_status", camera.get("recorder_state", "unknown"))
        self.service_status.set(f"{did}: {state}")
        if self.live_session_id is None and not self.live_starting:
            if state not in ("recording",):
                self._set_status(f"Camera offline / recorder: {state}")

    def _interval_iso(self):
        day = self.date_var.get().strip()
        start_clock = f"{self.start_hour_var.get()}:{self.start_minute_var.get()}"
        end_clock = f"{self.end_hour_var.get()}:{self.end_minute_var.get()}"
        tz = datetime.now().astimezone().tzinfo
        start = datetime.fromisoformat(f"{day}T{start_clock}").replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{day}T{end_clock}").replace(tzinfo=tz)
        if end <= start:
            raise ValueError("Giờ kết thúc phải sau giờ bắt đầu trong cùng ngày")
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _time_picker(parent, hour_var, minute_var):
        row = ttk.Frame(parent)
        ttk.Combobox(row, textvariable=hour_var, values=tuple(f"{n:02d}" for n in range(24)),
                     state="readonly", width=3).pack(side="left")
        ttk.Label(row, text=":").pack(side="left")
        ttk.Combobox(row, textvariable=minute_var, values=tuple(f"{n:02d}" for n in range(60)),
                     state="readonly", width=3).pack(side="left")
        return row

    def _open_date_picker(self):
        if self.date_picker is not None:
            try:
                if self.date_picker.winfo_exists():
                    self.date_picker.lift()
                    return
            except tk.TclError:
                pass
        selected = datetime.fromisoformat(self.date_var.get()).date()
        self.calendar_year, self.calendar_month = selected.year, selected.month
        popup = tk.Toplevel(self.window)
        self.date_picker = popup
        popup.title("Chọn ngày")
        popup.transient(self.window)
        popup.resizable(False, False)
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)
        header = ttk.Frame(popup, padding=8)
        header.pack(fill="x")
        ttk.Button(header, text="◀", width=3, command=lambda: self._move_calendar(-1)).pack(side="left")
        self.calendar_title = ttk.Label(header, anchor="center", width=18)
        self.calendar_title.pack(side="left", expand=True)
        ttk.Button(header, text="▶", width=3, command=lambda: self._move_calendar(1)).pack(side="right")
        self.calendar_grid = ttk.Frame(popup, padding=(8, 0, 8, 8))
        self.calendar_grid.pack()
        for col, label in enumerate(("T2", "T3", "T4", "T5", "T6", "T7", "CN")):
            ttk.Label(self.calendar_grid, text=label, width=4, anchor="center").grid(row=0, column=col)
        self._render_calendar()
        popup.grab_set()

    def _move_calendar(self, amount):
        month_index = self.calendar_year * 12 + self.calendar_month - 1 + amount
        self.calendar_year, month = divmod(month_index, 12)
        self.calendar_month = month + 1
        self._render_calendar()

    def _render_calendar(self):
        if not self.date_picker or not self.date_picker.winfo_exists():
            return
        self.calendar_title.configure(text=f"Tháng {self.calendar_month:02d}/{self.calendar_year}")
        for child in self.calendar_grid.grid_slaves():
            if int(child.grid_info().get("row", 0)) > 0:
                child.destroy()
        selected = datetime.fromisoformat(self.date_var.get()).date()
        for row_index, week in enumerate(pycalendar.monthcalendar(self.calendar_year, self.calendar_month), start=1):
            for col_index, day in enumerate(week):
                if day:
                    date_value = datetime(self.calendar_year, self.calendar_month, day).date()
                    style = "Accent.TButton" if date_value == selected else "TButton"
                    ttk.Button(self.calendar_grid, text=str(day), width=3, style=style,
                               command=lambda value=date_value: self._select_date(value)).grid(
                                   row=row_index, column=col_index, padx=1, pady=1
                               )

    def _select_date(self, value):
        self.date_var.set(value.isoformat())
        self.date_label_var.set(value.strftime("%d/%m/%Y"))
        if self.date_picker is not None:
            self.date_picker.destroy()
            self.date_picker = None

    @staticmethod
    def _format_local_timestamp(value):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            else:
                stamp = stamp.astimezone()
            raw_offset = stamp.strftime("%z")
            offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}" if len(raw_offset) == 5 else "UTC"
            return f"{stamp:%d/%m/%Y %H:%M:%S} ({offset})"
        except (AttributeError, TypeError, ValueError):
            return str(value or "?")

    def _selected_camera(self):
        did = self.camera_id.get().strip()
        if not did:
            raise ValueError("Hãy chọn camera")
        return did

    def start_live(self):
        if self.busy or self.live_starting or self.live_session_id or self._pump_running():
            return
        try:
            did = self._selected_camera()
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self.live_starting = True
        self.cancel_live.clear()
        self.busy = True
        self.camera_combo.configure(state="disabled")
        self._set_status("Đang tạo live session trên Server...")

        def work():
            status_data = self._request("/api/status", timeout=3)
            camera = next((item for item in status_data.get("cameras", [])
                           if item.get("camera_id") == did), None)
            if camera is None:
                raise RuntimeError("Camera chưa được cấu hình trên Camera Server")
            if camera.get("recorder_state") != "recording":
                raise RuntimeError(f"Camera offline / recorder đang ở trạng thái {camera.get('recorder_state', 'unknown')}")
            result = self._request("/api/live-sessions", "POST", {"device_id": did})
            session_id = result["session_id"]
            if self.cancel_live.is_set():
                self._request(f"/api/live-sessions/{session_id}", "DELETE")
                raise RuntimeError("Live session đã bị hủy")
            url = urljoin(self.base_url + "/", result["stream_url"].lstrip("/"))
            if self.cancel_live.is_set():
                self._request(f"/api/live-sessions/{session_id}", "DELETE")
                raise RuntimeError("Live session đã bị hủy")
            return did, session_id, url

        def done(value):
            self.busy = False
            self.live_starting = False
            did0, session_id, stream_url = value
            self.live_session_id = session_id
            self._start_live_pump(stream_url)
            self._set_status(f"LIVE MJPEG: {did0} qua Camera Server")

        def failed(error):
            self.busy = False
            self.live_starting = False
            self.camera_combo.configure(state="readonly")
            self._set_status(f"Live lỗi: {error}")

        self._run_worker(work, done, failed)

    def stop_live(self):
        session_id = self.live_session_id
        if not session_id:
            if self.live_starting:
                self._set_status("Đang khởi tạo live; chờ thao tác hoàn tất")
            return
        self.live_session_id = None
        self.live_closing = True
        self._stop_pump()
        self.camera_combo.configure(state="readonly")
        self._set_status("Đang đóng phiên Live trên Server...")

        def close_and_wait():
            request_error = None
            try:
                # Deleting the session closes the Server's MJPEG response and
                # releases the HTTP reader without stopping the recorder.
                self._request(f"/api/live-sessions/{session_id}", "DELETE", timeout=3)
            except Exception as exc:
                request_error = str(exc)
            pump = self.pump_thread
            if pump and pump is not threading.current_thread():
                pump.join(timeout=1.0)
            return request_error, bool(pump and pump.is_alive())

        def closed(result):
            request_error, pump_alive = result
            if not pump_alive:
                self.pump_thread = None
                self.live_closing = False
                self._set_status("Đã đóng Live; có thể chuyển sang xem footage. Ghi hình vẫn tiếp tục.")
            else:
                self._set_status("Live đã đóng trên Server; đang chờ reader kết thúc trước khi phát footage.")
            if request_error:
                self._set_status(f"Đã dừng Live cục bộ; Server báo: {request_error}")
            if self.playback_pending and not pump_alive:
                self.playback_pending = False
                self.window.after(0, self.start_playback)

        self._run_worker(close_and_wait, closed,
                         lambda error: self._set_status(f"Lỗi khi đóng Live: {error}"))

    def load_footage(self):
        if self.busy:
            return
        try:
            did = self._selected_camera()
            start, end = self._interval_iso()
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self.busy = True
        self._set_status("Đang tải manifest/timeline...")
        query = urlencode({"device_id": did, "start_time": start, "end_time": end})

        def done(manifest):
            self.busy = False
            self.manifest = manifest
            self._render_manifest(manifest)
            interval_start = datetime.fromisoformat(start)
            interval_end = datetime.fromisoformat(end)
            self.timeline_anchor = interval_start
            self.timeline_duration = (interval_end - interval_start).total_seconds()
            self.playback_gaps = self._compute_timeline_gaps(manifest, interval_start, interval_end)
            self._draw_timeline_marks()
            count = len(manifest.get("segments", []))
            self._set_status(f"Đã tải {count} segment; {len(manifest.get('gaps', []))} khoảng mất hình")
            if count == 0:
                self._set_status("Không có footage trong khoảng giờ đã chọn")

        def failed(error):
            self.busy = False
            self._set_status(f"Không tải được footage: {error}")

        self._run_worker(lambda: self._request("/api/playback/manifest?" + query), done, failed)

    @staticmethod
    def _compute_timeline_gaps(manifest, requested_start, requested_end):
        duration = (requested_end - requested_start).total_seconds()
        playable = []
        for item in manifest.get("segments", []):
            if not item.get("file_exists", True) or item.get("status") != "complete":
                continue
            segment_start = datetime.fromisoformat(item["start_time"])
            segment_end = datetime.fromisoformat(item["end_time"])
            left = max(requested_start, segment_start)
            right = min(requested_end, segment_end)
            if right > left:
                playable.append({"timeline_start": (left - requested_start).total_seconds(),
                                 "timeline_end": (right - requested_start).total_seconds()})
        gaps = []
        cursor = 0.0
        for segment in sorted(playable, key=lambda item: item["timeline_start"]):
            if segment["timeline_start"] > cursor:
                gaps.append({"timeline_start": cursor, "timeline_end": segment["timeline_start"]})
            cursor = max(cursor, segment["timeline_end"])
        if cursor < duration:
            gaps.append({"timeline_start": cursor, "timeline_end": duration})
        for item in manifest.get("gaps", []):
            gap_start = datetime.fromisoformat(item["start_time_utc"])
            gap_end = datetime.fromisoformat(item["end_time_utc"])
            left = max(0.0, (gap_start - requested_start).total_seconds())
            right = min(duration, (gap_end - requested_start).total_seconds())
            if right > left:
                gaps.append({"timeline_start": left, "timeline_end": right})
        merged = []
        for gap in sorted(gaps, key=lambda item: item["timeline_start"]):
            if merged and gap["timeline_start"] <= merged[-1]["timeline_end"]:
                merged[-1]["timeline_end"] = max(merged[-1]["timeline_end"], gap["timeline_end"])
            else:
                merged.append(gap)
        return merged

    def _render_manifest(self, manifest):
        self.footage_list.delete(0, tk.END)
        items = []
        for segment in manifest.get("segments", []):
            start = segment.get("start_time_local", segment.get("start_time", "?"))
            end = segment.get("end_time_local", segment.get("end_time", "?"))
            interval = f"{self._format_local_timestamp(start)}  →  {self._format_local_timestamp(end)}"
            if not segment.get("file_exists", True):
                label = f"MISSING  {interval} | file không còn tồn tại"
            else:
                label = f"VIDEO    {interval} | {self._bytes(segment.get('file_size', 0))}"
            items.append((segment.get("start_time", ""), label))
        for gap in manifest.get("gaps", []):
            start = gap.get("start_time_local", gap.get("start_time_utc", "?"))
            end = gap.get("end_time_local", gap.get("end_time_utc", "?"))
            interval = f"{self._format_local_timestamp(start)}  →  {self._format_local_timestamp(end)}"
            items.append((gap.get("start_time_utc", ""),
                          f"GAP      {interval} | Không có dữ liệu"))
        for _stamp, label in sorted(items, key=lambda item: item[0]):
            self.footage_list.insert(tk.END, label)

    def start_playback(self):
        if self.live_closing and self._pump_running() and not self.live_session_id:
            self.playback_pending = True
            self._set_status("Đang đóng Live; playback sẽ bắt đầu ngay khi bộ giải mã được giải phóng")
            return
        if self.busy or self.live_session_id or self.live_starting or self._pump_running():
            if self.live_session_id:
                self._set_status("Hãy đóng Live trước khi mở playback")
            elif self._pump_running():
                self._set_status("Đang giải phóng luồng Live; chờ một chút rồi phát footage")
            return
        try:
            did = self._selected_camera()
            start, end = self._interval_iso()
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self.busy = True
        self.cancel_playback.clear()
        self._set_status("Đang tải danh sách footage... ")
        query = urlencode({"device_id": did, "start_time": start, "end_time": end})

        def work():
            manifest = self._request("/api/playback/manifest?" + query)
            if self.cancel_playback.is_set():
                raise RuntimeError("Đã hủy playback")
            requested_start = datetime.fromisoformat(start)
            requested_end = datetime.fromisoformat(end)
            duration = (requested_end - requested_start).total_seconds()
            playable = []
            for item in manifest.get("segments", []):
                if not item.get("file_exists", True) or item.get("status") != "complete":
                    continue
                segment_start = datetime.fromisoformat(item["start_time"])
                segment_end = datetime.fromisoformat(item["end_time"])
                clip_start = max(requested_start, segment_start)
                clip_end = min(requested_end, segment_end)
                if clip_end <= clip_start:
                    continue
                playable.append({
                    "url": urljoin(self.base_url + "/", item["playback_url"].lstrip("/")),
                    "timeline_start": (clip_start - requested_start).total_seconds(),
                    "timeline_end": (clip_end - requested_start).total_seconds(),
                    "source_start": (clip_start - segment_start).total_seconds(),
                    "source_end": (clip_end - segment_start).total_seconds(),
                })
            gaps = self._compute_timeline_gaps(manifest, requested_start, requested_end)
            if not playable:
                raise RuntimeError("Không có footage trong khoảng đã chọn")
            # Play the server's validated MP4 segments directly. Exporting and
            # re-encoding a whole hour before showing the first frame was the
            # main source of the long startup delay.
            video_url = playable[0]["url"]
            try:
                capture = cv2.VideoCapture(
                    video_url, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                     cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1200],
                )
            except (TypeError, cv2.error):
                capture = cv2.VideoCapture(video_url, cv2.CAP_FFMPEG)
            if not capture.isOpened():
                capture.release()
                raise RuntimeError("Không mở được segment MP4; file có thể đã bị dọn hoặc bị lỗi")
            if playable[0]["source_start"] > 0:
                capture.set(cv2.CAP_PROP_POS_MSEC, playable[0]["source_start"] * 1000.0)
            if self.closed:
                capture.release()
                raise RuntimeError("Camera Center đã đóng")
            return manifest, capture, duration, playable, gaps

        def done(value):
            self.busy = False
            manifest, capture, duration, playable, gaps = value
            self.manifest = manifest
            self._render_manifest(manifest)
            self.playback_segments = playable
            self.playback_segment_index = 0
            self.playback_gaps = gaps
            self.timeline_anchor = datetime.fromisoformat(start)
            self._start_pump(capture, live=False, duration=duration)
            self._set_status("Đang phát khoảng giờ đã chọn")

        def failed(error):
            self.busy = False
            self._set_status(f"Playback lỗi / file không còn tồn tại: {error}")

        self._run_worker(work, done, failed)

    def _start_pump(self, capture, *, live, duration=0):
        self.pump_stop.clear()
        self.active_capture = capture
        with self.player_lock:
            self.player_state.update(frame=None, position=0.0, duration=duration,
                                     status="LIVE" if live else "PLAYBACK", paused=False, seek=None,
                                     speed=float(self.speed_var.get().rstrip("x")))
        self.pause_button.config(state="disabled" if live else "normal", text="Pause")
        if live:
            self.timeline_anchor = None
            self.timeline_time.set("Live")
            self.timeline_duration = 0.0
            self._draw_timeline_marks()
        elif self.timeline_anchor is not None:
            self._update_timeline_labels(duration)
        self.progress.configure(to=max(duration, 1.0))
        self.pump_thread = threading.Thread(target=self._pump_video, args=(capture, live),
                                            name="camera-video-decode", daemon=True)
        self.pump_thread.start()

    def _pump_running(self):
        return self.pump_thread is not None and self.pump_thread.is_alive()

    def _start_live_pump(self, url):
        """Read the MJPEG relay directly so OpenCV FFmpeg cannot queue stale frames."""
        self.pump_stop.clear()
        self.active_capture = None
        with self.player_lock:
            self.player_state.update(frame=None, position=0.0, duration=0.0,
                                     status="LIVE", paused=False, seek=None, speed=1.0)
        self.pause_button.config(state="disabled", text="Pause")
        self.timeline_anchor = None
        self.timeline_time.set("Live")
        self.timeline_duration = 0.0
        self._draw_timeline_marks()
        self.progress.configure(to=1.0)
        self.pump_thread = threading.Thread(target=self._pump_live_mjpeg, args=(url,),
                                            name="camera-live-mjpeg", daemon=True)
        self.pump_thread.start()

    def _pump_live_mjpeg(self, url):
        retry_delay = 0.4
        try:
            while not self.pump_stop.is_set():
                response = None
                try:
                    request = Request(url, headers={"Cache-Control": "no-cache"})
                    response = urlopen(request, timeout=4.0)
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(4.0)
                    buffer = bytearray()
                    read_chunk = getattr(response, "read1", response.read)
                    while not self.pump_stop.is_set():
                        chunk = read_chunk(65536)
                        if not chunk:
                            raise ConnectionError("Camera Server closed the MJPEG relay")
                        buffer.extend(chunk)
                        if len(buffer) > 8 * 1024 * 1024:
                            start = buffer.rfind(b"\xff\xd8")
                            buffer = bytearray(buffer[start:] if start >= 0 else b"")
                        while True:
                            start = buffer.find(b"\xff\xd8")
                            if start < 0:
                                if len(buffer) > 1:
                                    del buffer[:-1]
                                break
                            end = buffer.find(b"\xff\xd9", start + 2)
                            if end < 0:
                                if start:
                                    del buffer[:start]
                                break
                            jpeg = bytes(buffer[start:end + 2])
                            del buffer[:end + 2]
                            frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                            if frame is None:
                                continue
                            with self.player_lock:
                                self.player_state["frame"] = frame
                                self.player_state["status"] = "LIVE"
                            retry_delay = 0.4
                except Exception as exc:
                    if self.pump_stop.is_set():
                        break
                    with self.player_lock:
                        self.player_state["status"] = f"Live relay gián đoạn; đang nối lại ({exc})"
                    if self.pump_stop.wait(retry_delay):
                        break
                    retry_delay = min(retry_delay * 2, 3.2)
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
        finally:
            if self.pump_stop.is_set():
                with self.player_lock:
                    self.player_state["status"] = "Đã dừng video"
                    self.player_state["frame"] = None
            self._post(self._live_pump_finished)

    def _pump_video(self, capture, live):
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 20.0)
        delay = 1.0 / max(1.0, min(fps, 60.0))
        active_gap = next((gap for gap in self.playback_gaps
                           if gap["timeline_start"] <= 0 < gap["timeline_end"]), None) if not live else None
        playback_position = 0.0
        self._post(lambda: self.video_label.configure(text="", image=""))
        try:
            while not self.pump_stop.is_set():
                with self.player_lock:
                    seek = self.player_state.get("seek")
                    paused = self.player_state.get("paused", False)
                    speed = self.player_state.get("speed", 1.0) if not live else 1.0
                    if seek is not None:
                        self.player_state["seek"] = None
                if seek is not None:
                    if live:
                        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seek) * 1000.0)
                    else:
                        playback_position = max(0.0, min(float(seek), self.player_state["duration"]))
                        active_gap = next((gap for gap in self.playback_gaps
                                           if gap["timeline_start"] <= playback_position < gap["timeline_end"]), None)
                        if active_gap is not None:
                            with self.player_lock:
                                self.player_state["position"] = playback_position
                                self.player_state["frame"] = np.zeros((480, 640, 3), dtype=np.uint8)
                                self.player_state["status"] = "GAP: Không có dữ liệu"
                            continue
                        index = next((i for i, segment in enumerate(self.playback_segments)
                                      if segment["timeline_start"] <= playback_position < segment["timeline_end"]), None)
                        if index is None:
                            index = next((i for i, segment in enumerate(self.playback_segments)
                                          if segment["timeline_start"] >= playback_position),
                                         len(self.playback_segments) - 1)
                        segment = self.playback_segments[index]
                        if index != self.playback_segment_index:
                            replacement = cv2.VideoCapture(segment["url"], cv2.CAP_FFMPEG)
                            if replacement.isOpened():
                                capture.release()
                                capture = replacement
                                self.active_capture = capture
                                self.playback_segment_index = index
                            else:
                                replacement.release()
                        source_position = segment["source_start"] + max(
                            0.0, min(playback_position - segment["timeline_start"],
                                     segment["timeline_end"] - segment["timeline_start"])
                        )
                        capture.set(cv2.CAP_PROP_POS_MSEC, source_position * 1000.0)
                if paused:
                    self.pump_stop.wait(0.05)
                    continue
                if not live and active_gap is not None:
                    step = delay / max(0.25, min(speed, 4.0))
                    playback_position = min(active_gap["timeline_end"], playback_position + step)
                    with self.player_lock:
                        self.player_state["frame"] = np.zeros((480, 640, 3), dtype=np.uint8)
                        self.player_state["position"] = playback_position
                        self.player_state["status"] = "GAP: Không có dữ liệu"
                    if playback_position >= active_gap["timeline_end"]:
                        next_index = next((i for i, segment in enumerate(self.playback_segments)
                                           if segment["timeline_start"] >= active_gap["timeline_end"] - 0.1), None)
                        if next_index is None:
                            with self.player_lock:
                                self.player_state["status"] = "Đã phát hết khoảng footage"
                                self.player_state["paused"] = True
                            break
                        next_segment = self.playback_segments[next_index]
                        next_capture = cv2.VideoCapture(next_segment["url"], cv2.CAP_FFMPEG)
                        if not next_capture.isOpened():
                            next_capture.release()
                            with self.player_lock:
                                self.player_state["status"] = "Không mở được segment sau khoảng mất hình"
                                self.player_state["paused"] = True
                            break
                        capture.release()
                        capture = next_capture
                        capture.set(cv2.CAP_PROP_POS_MSEC, next_segment["source_start"] * 1000.0)
                        self.active_capture = capture
                        self.playback_segment_index = next_index
                        active_gap = None
                    self.pump_stop.wait(step)
                    continue
                ok, frame = capture.read()
                if ok and not live:
                    current_segment = self.playback_segments[self.playback_segment_index]
                    if capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 > current_segment["source_end"]:
                        ok = False
                if self.pump_stop.is_set():
                    break
                if not ok and not live and self.playback_segments:
                    next_index = self.playback_segment_index + 1
                    if next_index < len(self.playback_segments):
                        next_segment = self.playback_segments[next_index]
                        current_segment = self.playback_segments[self.playback_segment_index]
                        gap = next((item for item in self.playback_gaps
                                    if item["timeline_start"] >= current_segment["timeline_end"] - 0.1
                                    and item["timeline_end"] <= next_segment["timeline_start"] + 0.1), None)
                        if gap is not None:
                            active_gap = gap
                            playback_position = gap["timeline_start"]
                            with self.player_lock:
                                self.player_state["position"] = playback_position
                                self.player_state["frame"] = np.zeros((480, 640, 3), dtype=np.uint8)
                                self.player_state["status"] = "GAP: Không có dữ liệu"
                            continue
                        next_capture = cv2.VideoCapture(next_segment["url"], cv2.CAP_FFMPEG)
                        if next_capture.isOpened():
                            capture.release()
                            capture = next_capture
                            self.active_capture = capture
                            self.playback_segment_index = next_index
                            ok, frame = capture.read()
                        else:
                            next_capture.release()
                    else:
                        current_segment = self.playback_segments[self.playback_segment_index]
                        gap = next((item for item in self.playback_gaps
                                    if item["timeline_start"] >= current_segment["timeline_end"] - 0.1), None)
                        if gap is not None:
                            active_gap = gap
                            playback_position = gap["timeline_start"]
                            with self.player_lock:
                                self.player_state["position"] = playback_position
                                self.player_state["frame"] = np.zeros((480, 640, 3), dtype=np.uint8)
                                self.player_state["status"] = "GAP: Không có dữ liệu"
                            continue
                if not ok:
                    with self.player_lock:
                        self.player_state["status"] = "Đã phát hết khoảng footage"
                        self.player_state["paused"] = True
                    break
                with self.player_lock:
                    self.player_state["frame"] = frame
                    if not live:
                        segment = self.playback_segments[self.playback_segment_index]
                        local = float(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0)
                        self.player_state["position"] = segment["timeline_start"] + max(
                            0.0, local - segment["source_start"]
                        )
                        playback_position = self.player_state["position"]
                    self.player_state["status"] = "LIVE" if live else "PLAYBACK"
                if not live:
                    self.pump_stop.wait(delay / max(0.25, min(speed, 4.0)))
        except Exception as exc:
            # Releasing a capture while read() is blocked can raise cv2.error
            # on Windows. This expected shutdown race should not look like a
            # decoding failure.
            if not self.pump_stop.is_set():
                with self.player_lock:
                    self.player_state["status"] = f"Lỗi giải mã video: {exc}"
        finally:
            try:
                capture.release()
            except Exception:
                pass
            if self.pump_stop.is_set():
                with self.player_lock:
                    self.player_state["status"] = "Đã dừng video"
                    self.player_state["frame"] = None
            if self.active_capture is capture:
                self.active_capture = None
            if live:
                self._post(self._live_pump_finished)
            if not live:
                self._post(lambda: self.pause_button.config(state="disabled", text="Pause"))
            if not live and self.playback_export_id:
                export_id = self.playback_export_id
                self.playback_export_id = None
                try:
                    self._request(f"/api/exports/{export_id}", "DELETE", timeout=3)
                except Exception:
                    pass

    def _live_pump_finished(self):
        if self._pump_running():
            self.window.after(100, self._live_pump_finished)
            return
        self.pump_thread = None
        self.live_closing = False
        if self.playback_pending:
            self.playback_pending = False
            self.start_playback()

    def _ui_tick(self):
        if self.closed:
            return
        while True:
            try:
                event = self.ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                event()
            except tk.TclError:
                pass
        with self.player_lock:
            frame = self.player_state.get("frame")
            self.player_state["frame"] = None
            position = self.player_state.get("position", 0.0)
            duration = self.player_state.get("duration", 0.0)
            status = self.player_state.get("status", "")
            paused = self.player_state.get("paused", False)
        if frame is not None:
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb)
                image.thumbnail((720, 420))
                if self.video_photo is None or self.video_photo.width() != image.width or self.video_photo.height() != image.height:
                    self.video_photo = ImageTk.PhotoImage(image=image)
                    self.video_label.configure(image=self.video_photo, text="")
                    self.video_label.image = self.video_photo
                else:
                    # Reuse the Tk image object instead of allocating/destroying
                    # one image for every frame, which is costly on Windows.
                    self.video_photo.paste(image)
                self.ui_frames_presented += 1
            except Exception:
                pass
        if duration > 0:
            self.updating_scale = True
            self.progress.configure(to=duration)
            self.progress.set(min(duration, max(0, position)))
            self.updating_scale = False
            if self.timeline_anchor is not None:
                self.timeline_time.set(
                    (self.timeline_anchor + timedelta(seconds=max(0.0, position))).astimezone().strftime(
                        "Thời gian: %Y-%m-%d %H:%M:%S"
                    ) + f"   |   {self._format_clock(position)} / {self._format_clock(duration)}"
                )
        if status:
            if status == "LIVE":
                now = time.monotonic()
                elapsed = now - self.ui_fps_window_started
                if elapsed >= 1.0:
                    self.ui_fps = self.ui_frames_presented / elapsed
                    self.ui_frames_presented = 0
                    self.ui_fps_window_started = now
                camera = self.camera_states.get(self.camera_id.get(), {})
                source_fps = float(camera.get("live_source_fps", 0.0) or 0.0)
                self.media_status.set(f"LIVE | Nguồn Server: {source_fps:.1f} fps | Hiển thị: {self.ui_fps:.1f} fps")
            else:
                self.media_status.set(status + (" | PAUSE" if paused and status == "PLAYBACK" else ""))
        try:
            # Poll faster than the target frame period; render only the newest
            # available frame so a slow Tk redraw never queues stale video.
            self.window.after(16, self._ui_tick)
        except tk.TclError:
            pass

    def toggle_pause(self):
        if not self._pump_running() or self.live_session_id:
            return
        with self.player_lock:
            paused = not self.player_state.get("paused", False)
            self.player_state["paused"] = paused
        self.pause_button.config(text="Play" if paused else "Pause")

    @staticmethod
    def _format_clock(seconds):
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _update_timeline_labels(self, duration):
        self.timeline_duration = max(0.0, float(duration))
        self._draw_timeline_marks()

    def _draw_timeline_marks(self):
        canvas = getattr(self, "timeline_marks", None)
        if canvas is None:
            return
        canvas.delete("all")
        duration = self.timeline_duration
        if self.timeline_anchor is None or duration <= 0:
            return
        width = max(1, canvas.winfo_width())
        pad = 24
        usable_width = max(1, width - 2 * pad)
        # Place a label at the start, then every 30 minutes, and at the end.
        offsets = list(range(0, int(duration), 30 * 60))
        if not offsets or offsets[-1] != duration:
            offsets.append(duration)
        for index, elapsed in enumerate(offsets):
            x = pad + usable_width * elapsed / duration
            label = (self.timeline_anchor + timedelta(seconds=elapsed)).astimezone().strftime("%H:%M")
            anchor = "w" if index == 0 else "e" if index == len(offsets) - 1 else "center"
            canvas.create_line(x, 0, x, 6)
            canvas.create_text(x, 17, text=label, anchor=anchor)
        for gap in getattr(self, "playback_gaps", []):
            left = max(0.0, min(duration, gap["timeline_start"]))
            right = max(left, min(duration, gap["timeline_end"]))
            x1 = pad + usable_width * left / duration
            x2 = pad + usable_width * right / duration
            if x2 - x1 < 1.0:
                canvas.create_line((x1 + x2) / 2, 7, (x1 + x2) / 2, 24,
                                   fill="#b36b00", width=2)
            else:
                canvas.create_rectangle(x1, 7, x2, 24, fill="#f2c66d",
                                        outline="#a66c00", stipple="gray50")
                canvas.create_line(x1, 6, x1, 24, fill="#8a4b00", width=1)
                canvas.create_line(x2, 6, x2, 24, fill="#8a4b00", width=1)
                if x2 - x1 >= 58:
                    canvas.create_text((x1 + x2) / 2, 15, text="Mất hình",
                                       fill="#6a4300", font=("TkDefaultFont", 8, "bold"))

    def _set_speed(self, _event=None):
        try:
            speed = float(self.speed_var.get().rstrip("x"))
        except ValueError:
            speed = 1.0
        with self.player_lock:
            self.player_state["speed"] = speed

    def seek_to_slider(self, _event=None):
        if self.updating_scale or not self._pump_running() or self.live_session_id:
            return
        if _event is not None:
            width = max(1, self.progress.winfo_width())
            fraction = max(0.0, min(1.0, (_event.x - 8) / max(1, width - 16)))
            duration = self.player_state.get("duration", 0.0)
            self.progress.set(duration * fraction)
        with self.player_lock:
            self.player_state["seek"] = float(self.progress.get())

    def jump(self, seconds):
        if not self._pump_running() or self.live_session_id:
            return
        with self.player_lock:
            target = self.player_state.get("position", 0.0) + seconds
            duration = self.player_state.get("duration", 0.0)
            self.player_state["seek"] = max(0.0, min(target, duration))

    def _stop_pump(self):
        self.pump_stop.set()
        self.pause_button.config(state="disabled")
        try:
            self.video_label.configure(image="", text="Video đã dừng")
            self.video_label.image = None
            self.video_photo = None
        except tk.TclError:
            pass

    def stop_playback(self):
        if not self.live_session_id:
            if self.busy:
                self.cancel_playback.set()
                self._set_status("Đang hủy yêu cầu playback...")
                return
            self._stop_pump()
            capture = self.active_capture
            if capture is not None:
                threading.Thread(target=capture.release, name="close-playback-decoder",
                                 daemon=True).start()
            self._set_status("Playback đã dừng; đã đóng khung phát và giải phóng bộ giải mã")

    def close(self):
        if self.closed:
            return
        session_id = self.live_session_id
        self.cancel_live.set()
        self.cancel_playback.set()
        self.live_session_id = None
        self._stop_pump()
        self.closed = True
        if session_id:
            threading.Thread(target=lambda: self._request(f"/api/live-sessions/{session_id}", "DELETE"),
                             name="close-live-session", daemon=True).start()
        try:
            self.window.destroy()
        except tk.TclError:
            pass


def open_camera_center():
    global camera_center
    try:
        if camera_center is not None and camera_center.window.winfo_exists():
            camera_center.window.deiconify()
            camera_center.window.lift()
            return
    except tk.TclError:
        camera_center = None
    camera_center = CameraCenter()


def update_camera_endpoint(data, did=None):
    """Update endpoint only for a camera that the user has confirmed."""
    device_id = did or data.get("device_id") or "ESP32-CAM-01"
    ip = str(data.get("ip") or "").strip()
    stream_url = str(data.get("stream") or "").strip()
    status_url = str(data.get("status_url") or "").strip()

    # Discovery/status can arrive before confirmation. Do not expose its IP
    # in the global stream panel until the user confirms the camera.
    if device_id not in profiles or str(profiles[device_id].get("type", "")).lower() != "camera":
        return

    if ip:
        profiles[device_id]["ip"] = ip
        profiles[device_id]["last_seen"] = ip
    if stream_url:
        profiles[device_id]["stream_url"] = stream_url
    if status_url:
        profiles[device_id]["status_url"] = status_url
    if data.get("stream_port") is not None:
        profiles[device_id]["stream_port"] = int(data["stream_port"])
    if data.get("http_port") is not None:
        profiles[device_id]["http_port"] = int(data["http_port"])
    save_profiles()
    refresh_camera_stream_panel()
    if ip:
        root.after(100, lambda did=device_id: auto_start_camera_stream(did))


def message(c, userdata, msg):
    raw = msg.payload.decode(errors="replace")
    log(f"[{msg.topic}] {raw}")

    if msg.topic == DISCOVERY_TOPIC or msg.topic == REGISTER_TOPIC:
        # Only live discovery packets can populate the nearby-device list.
        # Retained announce packets may belong to devices that are already off.
        if msg.retain:
            log(f"[DISCOVERY] Ignore retained packet: {msg.topic}")
            return
        try:
            data = json.loads(raw)
            did = data.get("device_id")
            if did:
                if did not in profiles:
                    touch_pending_device(did, data)
                    log(f"[DISCOVERY] Thiết bị gần: {did} ({data.get('type', 'unknown')})")
                else:
                    profiles[did]["last_seen"] = data.get("ip", "")
                    if data.get("config_topic"):
                        profiles[did]["config_topic"] = data["config_topic"]
                    if data.get("ip"):
                        profiles[did]["ip"] = data["ip"]
                    if data.get("stream"):
                        profiles[did]["stream_url"] = data["stream"]
                    if data.get("status_url"):
                        profiles[did]["status_url"] = data["status_url"]
                    if data.get("stream_port") is not None:
                        profiles[did]["stream_port"] = int(data["stream_port"])
                    save_profiles()
                    if str(data.get("type", "")).lower() == "camera":
                        update_camera_endpoint(data, did)
                root.after(0, refresh_device_list)
        except Exception as e:
            log(f"[DISCOVERY] ERROR: {e}")
        return

    if msg.topic.endswith("/config_ack"):
        try:
            data = json.loads(raw)
            did = data.get("device_id")
            if did in profiles:
                version = int(data.get("version", profiles[did].get("version", 0)))
                profiles[did]["synced_version"] = version
                profiles[did]["device_version"] = version
                profiles[did]["sync_status"] = data.get("status", "UNKNOWN")
                save_profiles()
                root.after(0, refresh_device_list)
                log(f"[CONFIG ACK] {did}: version={version} status={data.get('status', 'UNKNOWN')}")
        except Exception as e:
            log(f"[CONFIG ACK] ERROR: {e}")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    did = data.get("device_id")

    if msg.topic in (T["rs"], T["rt"]):
        did = did or "ESP32-RELAY-01"

        # If the App missed announce/register, a LIVE relay status/telemetry
        # packet can still make the device appear in the nearby list.
        # Retained status must never be used for discovery.
        if not msg.retain and data.get("online") is not False and did not in profiles:
            touch_pending_device(did, {
                "device_id": did,
                "type": "relay",
                "name": data.get("name") or did,
                "room": data.get("room") or "",
                "ip": data.get("ip", ""),
                "config_topic": f"smarthome/device/relay01/config",
            })
            log(f"[DISCOVERY] Live relay signal: {did}")
            root.after(0, refresh_device_list)

        if did in profiles and "profile_version" in data:
            profiles[did]["device_version"] = int(data["profile_version"])
        if "online" in data:
            update_card(did, "online", "ONLINE" if data["online"] else "OFFLINE")
        for i in range(1, 5):
            key = f"relay{i}"
            if key in data:
                update_card(did, key, "ON" if data[key] else "OFF")
        if isinstance(data.get("channels"), list):
            for item in data["channels"]:
                try:
                    ch = int(item.get("id"))
                    if 1 <= ch <= 4 and "state" in item:
                        update_card(did, f"relay{ch}", "ON" if item["state"] else "OFF")
                except Exception:
                    pass
        return

    if msg.topic in (T["fs"], T["ft"]):
        did = did or "ESP32-FAN-01"

        # Do not rediscover a powered-off fan from retained status.
        # Only live MQTT traffic can populate the nearby-device list.
        if not msg.retain and data.get("online") is not False and did not in profiles:
            touch_pending_device(did, {
                "device_id": did,
                "type": "fan",
                "name": data.get("name") or did,
                "room": data.get("room") or "",
                "ip": data.get("ip", ""),
                "config_topic": f"smarthome/device/fan01/config",
            })
            log(f"[DISCOVERY] Live fan signal: {did}")
            root.after(0, refresh_device_list)

        if did in profiles and "profile_version" in data:
            profiles[did]["device_version"] = int(data["profile_version"])
        if "online" in data:
            update_card(did, "online", "ONLINE" if data["online"] else "OFFLINE")
        if "temperature" in data and data["temperature"] is not None:
            update_card(did, "temperature", f'{float(data["temperature"]):.1f} °C')
        if "humidity" in data and data["humidity"] is not None:
            update_card(did, "humidity", f'{float(data["humidity"]):.1f} %')
        if "fan_level" in data:
            update_card(did, "fan_level", str(data["fan_level"]))
        if "fan_percent" in data:
            update_card(did, "fan_percent", f'{data["fan_percent"]}%')
        return

    if msg.topic == T["cs"]:
        did = did or "ESP32-CAM-01"

        # Retained status is allowed to refresh the IP of an already-added
        # camera, but it must never create a new nearby device.
        if not msg.retain and data.get("online") is not False and did not in profiles:
            nearby = {
                "device_id": did,
                "type": "camera",
                "name": data.get("name") or did,
                "room": data.get("room") or "",
                "ip": data.get("ip", ""),
                "stream": data.get("stream", ""),
                "status_url": data.get("status_url", ""),
                "stream_port": data.get("stream_port", 81),
                "http_port": data.get("http_port", 80),
                "config_topic": data.get("config_topic") or f"smarthome/device/cam01/config",
                "security_topic": T["csec"],
            }
            touch_pending_device(did, nearby)
            log(f"[DISCOVERY] Live camera signal: {did} IP={data.get('ip', '')}")
            root.after(0, refresh_device_list)

        update_camera_endpoint(data, did)

        if "online" in data:
            update_card(did, "online", "ONLINE" if data["online"] else "OFFLINE")
        if "security_state" in data:
            update_card(did, "security", str(data["security_state"]))
        if "ip" in data:
            update_card(did, "ip", str(data["ip"]))
        return

    if msg.topic == T["csec"]:
        did = did or "ESP32-CAM-01"
        state = data.get("state") or data.get("security_state")
        if state is not None:
            update_card(did, "security", str(state))


# ============================================================
# MQTT CONNECTION
# ============================================================

def connect():

    global client

    try:

        # Disconnect old client
        if client:

            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

        # Paho MQTT 2.x
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="SmartHome-MQTT-TestConsole"
        )

        client.on_connect = connected
        client.on_disconnect = disconnected
        client.on_message = message

        broker_ip = broker.get().strip()
        broker_port = int(port.get())

        log(f"CONNECTING {broker_ip}:{broker_port}")

        client.connect(
            broker_ip,
            broker_port,
            60
        )

        client.loop_start()

    except Exception as e:

        messagebox.showerror(
            "MQTT",
            str(e)
        )

        log(f"ERROR: {e}")


# ============================================================
# MQTT PUBLISH
# ============================================================

def pub(topic, payload, retain=False):

    if not client or not client.is_connected():

        messagebox.showwarning(
            "MQTT",
            "Connect to broker first."
        )

        return

    result = client.publish(
        topic,
        payload,
        qos=0,
        retain=retain
    )

    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        log(f"PUB [{topic}] {payload}")
    else:
        log(
            f"PUB FAILED [{topic}] "
            f"rc={result.rc}"
        )


def pub_reliable(topic, payload, retain=False):
    """Publish a critical retained profile command with QoS 1."""
    if not client or not client.is_connected():
        messagebox.showwarning(
            "MQTT",
            "Connect to broker first."
        )
        return False

    result = client.publish(
        topic,
        payload,
        qos=1,
        retain=retain
    )

    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        log(f"PUB FAILED [{topic}] rc={result.rc}")
        return False

    try:
        result.wait_for_publish(timeout=3.0)
    except Exception as e:
        log(f"PUB WAIT ERROR [{topic}]: {e}")
        return False

    log(f"PUB QOS1 [{topic}] {payload}")
    return True


# ============================================================
# RELAY
# ============================================================

def relay(device_id, command=None, channel=None, state=None):
    """Control a relay device. Keeps the old relay(command) API working."""
    if command is None:
        command = device_id
        device_id = "ESP32-RELAY-01"

    profile = profiles.get(device_id, {})
    topic = profile.get("control_topic") or T["rc"]

    if channel is not None:
        payload = {
            "action": "SET_CHANNEL",
            "channel": int(channel),
            "state": bool(state)
        }
    elif command in ("ALL_ON", "ALL_OFF"):
        payload = {
            "action": "SET_ALL",
            "state": command == "ALL_ON"
        }
    else:
        # Backward compatibility with the original 2-channel firmware.
        payload = command

    pub(topic, json.dumps(payload) if isinstance(payload, dict) else payload)


# ============================================================
# FAN
# ============================================================

def fan(level):

    payload = {
        "action": "SET_LEVEL",
        "level": level
    }

    pub(
        T["fc"],
        json.dumps(payload)
    )


# ============================================================
# CAMERA SECURITY STATE
# ============================================================

def camstate(state):

    payload = {
        "action": "SET_SECURITY_STATE",
        "state": state
    }

    pub(
        T["csec"],
        json.dumps(payload)
    )


# ============================================================
# QUARANTINE
# ============================================================

def quarantine(device):

    payload = {
        "device_id": device,
        "action": "QUARANTINE",
        "reason": "Manual test"
    }

    topic = f"smarthome/security/{device}"

    pub(
        topic,
        json.dumps(payload)
    )


# ============================================================
# RELEASE
# ============================================================

def release(device):

    payload = {
        "device_id": device,
        "action": "RELEASE",
        "reason": "Manual test"
    }

    topic = f"smarthome/security/{device}"

    pub(
        topic,
        json.dumps(payload)
    )

def start_camera_stream():
    # Kept for compatibility; the recorder is the ESP32's only stream client.
    auto_start_camera_stream()


def stop_camera_stream():
    # Removing the device from AppDemo never stops the independent recorder.
    refresh_camera_stream_panel()

# ============================================================
# MAIN WINDOW
# ============================================================

root = tk.Tk()
root.after(DISCOVERY_CLEANUP_INTERVAL_MS, cleanup_stale_pending_devices)

root.title(
    "Smart Home MQTT Test Console"
)

root.geometry("1000x800")

# Scrollable page for the full application content.
page_canvas = tk.Canvas(root, highlightthickness=0)
page_scrollbar = ttk.Scrollbar(root, orient="vertical", command=page_canvas.yview)
page_canvas.configure(yscrollcommand=page_scrollbar.set)
page_scrollbar.pack(side="right", fill="y")
page_canvas.pack(side="left", fill="both", expand=True)

content_frame = ttk.Frame(page_canvas)
content_window = page_canvas.create_window((0, 0), window=content_frame, anchor="nw")

def update_page_scroll_region(_event=None):
    page_canvas.configure(scrollregion=page_canvas.bbox("all"))

def resize_page_content(event):
    page_canvas.itemconfigure(content_window, width=event.width)

def scroll_page(event):
    if event.delta:
        page_canvas.yview_scroll(int(-event.delta / 120), "units")

content_frame.bind("<Configure>", update_page_scroll_region)
page_canvas.bind("<Configure>", resize_page_content)
page_canvas.bind_all("<MouseWheel>", scroll_page)


# ============================================================
# STYLE
# ============================================================

style = ttk.Style()

try:
    style.configure(
        "Title.TLabel",
        font=("Segoe UI", 15, "bold")
    )
except Exception:
    pass


# ============================================================
# HEADER
# ============================================================

head = ttk.Frame(
    content_frame,
    padding=10
)

head.pack(
    fill="x"
)

ttk.Label(
    head,
    text="Smart Home MQTT Test Console",
    style="Title.TLabel"
).pack(
    side="left"
)


# ============================================================
# MQTT BROKER
# ============================================================

cf = ttk.LabelFrame(
    content_frame,
    text="MQTT Broker",
    padding=10
)

cf.pack(
    fill="x",
    padx=10,
    pady=5
)


ttk.Label(
    cf,
    text="Broker IP"
).grid(
    row=0,
    column=0
)


broker = ttk.Entry(
    cf,
    width=22
)

broker.insert(
    0,
    DEFAULT_BROKER
)

broker.grid(
    row=0,
    column=1,
    padx=5
)


ttk.Label(
    cf,
    text="Port"
).grid(
    row=0,
    column=2
)


port = ttk.Entry(
    cf,
    width=8
)

port.insert(
    0,
    str(DEFAULT_PORT)
)

port.grid(
    row=0,
    column=3,
    padx=5
)


ttk.Button(
    cf,
    text="CONNECT",
    command=connect
).grid(
    row=0,
    column=4,
    padx=10
)


conn = ttk.Label(
    cf,
    text="OFFLINE"
)

conn.grid(
    row=0,
    column=5
)


# ============================================================
# BODY - DYNAMIC DEVICE CARDS
# ============================================================

body = ttk.Frame(content_frame, padding=10)
body.pack(fill="both", expand=True)

cards_title = ttk.Frame(body)
cards_title.pack(fill="x", pady=(0, 5))
ttk.Label(
    cards_title,
    text="Thiết bị của tôi",
    font=("Segoe UI", 12, "bold")
).pack(side="left")

# Device cards are created only after the user adds a discovered device.
device_cards_frame = ttk.Frame(body)
device_cards_frame.pack(fill="both", expand=True)
device_widgets = {}


def create_device_card(device_id, profile):
    dtype = profile.get("type", "unknown")
    card = ttk.LabelFrame(
        device_cards_frame,
        text=f"{profile.get('name', device_id)}  |  {dtype.upper()}",
        padding=10
    )
    card.pack(fill="x", padx=2, pady=5)

    widgets = {}
    ttk.Label(card, text=f"Device ID: {device_id}").grid(row=0, column=0, columnspan=5, sticky="w")
    ttk.Label(card, text="Status").grid(row=1, column=0, sticky="w", pady=3)
    widgets["online"] = ttk.Label(card, text="OFFLINE")
    widgets["online"].grid(row=1, column=1, sticky="w")

    if dtype == "relay":
        channels = profile.get("settings", {}).get("channels", [])
        for i in range(4):
            c = channels[i] if i < len(channels) and isinstance(channels[i], dict) else {}
            ch_id = i + 1
            ch_name = c.get("name") or f"Relay {ch_id}"
            room_name = c.get("room") or "Chưa gán phòng"
            mode_name = c.get("automation_mode") or c.get("mode") or "manual"
            ttk.Label(card, text=f"CH{ch_id} - {ch_name}").grid(row=2+i, column=0, sticky="w", pady=2)
            ttk.Label(card, text=f"[{room_name}] {mode_name}").grid(row=2+i, column=1, sticky="w", pady=2)
            widgets[f"relay{ch_id}"] = ttk.Label(card, text="OFF")
            widgets[f"relay{ch_id}"].grid(row=2+i, column=2, sticky="w")
            ttk.Button(card, text="ON", command=lambda d=device_id, ch=ch_id: relay(d, channel=ch, state=True)).grid(row=2+i, column=3, padx=2)
            ttk.Button(card, text="OFF", command=lambda d=device_id, ch=ch_id: relay(d, channel=ch, state=False)).grid(row=2+i, column=4, padx=2)
        ttk.Button(card, text="ALL ON", command=lambda d=device_id: relay(d, command="ALL_ON")).grid(row=6, column=3, pady=6)
        ttk.Button(card, text="ALL OFF", command=lambda d=device_id: relay(d, command="ALL_OFF")).grid(row=6, column=4, pady=6)

    elif dtype == "fan":
        ttk.Label(card, text="Temperature").grid(row=2, column=0, sticky="w")
        widgets["temperature"] = ttk.Label(card, text="-- °C")
        widgets["temperature"].grid(row=2, column=1, sticky="w")
        ttk.Label(card, text="Humidity").grid(row=3, column=0, sticky="w")
        widgets["humidity"] = ttk.Label(card, text="-- %")
        widgets["humidity"].grid(row=3, column=1, sticky="w")
        ttk.Label(card, text="Fan level").grid(row=4, column=0, sticky="w")
        widgets["fan_level"] = ttk.Label(card, text="0")
        widgets["fan_level"].grid(row=4, column=1, sticky="w")
        ttk.Label(card, text="Output").grid(row=5, column=0, sticky="w")
        widgets["fan_percent"] = ttk.Label(card, text="0%")
        widgets["fan_percent"].grid(row=5, column=1, sticky="w")
        for col, level in enumerate([0, 1, 2, 3], start=2):
            ttk.Button(
                card,
                text="OFF" if level == 0 else f"LEVEL {level}",
                command=lambda x=level: fan(x)
            ).grid(row=6, column=col, padx=3, pady=6)

    elif dtype == "camera":
        global streamf, camera_ip, stream_status, camera_view
        ttk.Label(card, text="IP").grid(row=2, column=0, sticky="w")
        widgets["ip"] = ttk.Label(card, text=profile.get("ip", "--"))
        widgets["ip"].grid(row=2, column=1, sticky="w")
        ttk.Label(card, text="Security").grid(row=3, column=0, sticky="w")
        widgets["security"] = ttk.Label(card, text="UNKNOWN")
        widgets["security"].grid(row=3, column=1, sticky="w")
        for col, state in enumerate(["NORMAL", "SUSPICIOUS", "ATTACKED"], start=2):
            ttk.Button(
                card,
                text=state,
                command=lambda x=state: camstate(x)
            ).grid(row=3, column=col, padx=3, pady=6)

        ttk.Label(card, text="Recording Service").grid(row=4, column=0, sticky="w", padx=5, pady=(8, 3))
        ttk.Label(card, text="Camera IP (MQTT)").grid(row=5, column=0, padx=5, pady=5, sticky="e")
        camera_ip = tk.StringVar(value="")
        ttk.Entry(card, textvariable=camera_ip, width=20, state="readonly").grid(row=5, column=1, padx=5, pady=5, sticky="w")
        stream_status = ttk.Label(card, text="OFFLINE")
        stream_status.grid(row=5, column=2, padx=10, sticky="w")
        camera_view = ttk.Label(card, text="Video is recorded by the Camera Server", anchor="center", justify="center")
        camera_view.grid(row=6, column=0, columnspan=5, padx=5, pady=5)
        ttk.Button(card, text="CAMERA CENTER: LIVE / PLAYBACK", command=open_camera_center).grid(
            row=7, column=0, columnspan=5, sticky="ew", padx=5, pady=4
        )
        streamf = card

    ttk.Button(card, text="EDIT PROFILE", command=lambda d=device_id: edit_profile(d)).grid(
        row=10, column=0, pady=(8, 0), sticky="w"
    )
    ttk.Button(card, text="SYNC PROFILE", command=lambda d=device_id: sync_profile(d)).grid(
        row=10, column=1, pady=(8, 0), sticky="w"
    )
    ttk.Button(card, text="QUARANTINE", command=lambda d=device_id: quarantine(device_short_id(d))).grid(
        row=10, column=2, pady=(8, 0)
    )
    ttk.Button(card, text="RELEASE", command=lambda d=device_id: release(device_short_id(d))).grid(
        row=10, column=3, pady=(8, 0)
    )
    ttk.Button(card, text="XÓA THIẾT BỊ", command=lambda d=device_id: delete_device(d)).grid(
        row=10, column=4, pady=(8, 0), padx=(5, 0)
    )

    device_widgets[device_id] = widgets
    return card


def refresh_device_cards(force=False):
    if "device_cards_frame" not in globals():
        return
    if not force and set(device_widgets) == set(profiles):
        return
    for child in device_cards_frame.winfo_children():
        child.destroy()
    device_widgets.clear()
    if not profiles:
        ttk.Label(
            device_cards_frame,
            text="Chưa có thiết bị. Nhấn 'THÊM THIẾT BỊ' để tìm thiết bị gần bạn.",
            foreground="gray"
        ).pack(pady=35)
        return
    for did, profile in sorted(profiles.items()):
        create_device_card(did, profile)


# ============================================================
# Camera playback is started and stopped explicitly in Camera Center.

# DEVICE MANAGER / ADD DEVICE
# ============================================================

manager = ttk.LabelFrame(content_frame, text="Quản lý thiết bị", padding=8)
manager.pack(fill="x", padx=10, pady=5)

manager_top = ttk.Frame(manager)
manager_top.pack(fill="x")

ttk.Button(
    manager_top,
    text="THÊM THIẾT BỊ",
    command=open_add_device_dialog
).pack(side="left", padx=(0, 8))

ttk.Label(
    manager_top,
    text="Chọn loại → quét thiết bị gần → chọn thiết bị → thêm vào hệ thống",
    foreground="gray"
).pack(side="left")

# Internal profile table is kept for editing/sync compatibility.
# It is not used as the device-card area.
device_tree = ttk.Treeview(
    manager,
    columns=("id", "type", "name", "room", "version"),
    show="headings",
    height=3
)
for col, title, width in [
    ("id", "Device ID", 170),
    ("type", "Type", 70),
    ("name", "Name", 150),
    ("room", "Room", 120),
    ("version", "Ver", 45)
]:
    device_tree.heading(col, text=title)
    device_tree.column(col, width=width)
device_tree.pack(fill="x", pady=(8, 0))

manager_actions = ttk.Frame(manager)
manager_actions.pack(fill="x", pady=(5, 0))
ttk.Button(manager_actions, text="EDIT SELECTED", command=edit_selected_profile).pack(side="left", padx=(0, 5))
ttk.Button(manager_actions, text="SYNC SELECTED", command=sync_selected_profile).pack(side="left", padx=5)
def delete_selected_profile():
    selection = device_tree.selection()
    if not selection:
        messagebox.showwarning("Xóa thiết bị", "Hãy chọn thiết bị cần xóa.")
        return
    values = device_tree.item(selection[0], "values")
    if not values:
        return
    delete_device(str(values[0]))

ttk.Button(manager_actions, text="XÓA SELECTED", command=delete_selected_profile).pack(side="left", padx=5)
ttk.Button(manager_actions, text="REFRESH", command=refresh_device_list).pack(side="left", padx=5)

# Discovery cache. It is intentionally not shown as a permanent device card.
pending_list = tk.Listbox(manager, height=1)
pending_list.pack_forget()

# ============================================================
# MQTT LOG
# ============================================================

lf = ttk.LabelFrame(
    content_frame,
    text="MQTT Log",
    padding=5
)

lf.pack(
    fill="both",
    expand=True,
    padx=10,
    pady=5
)


logbox = tk.Text(
    lf,
    height=12,
    font=("Consolas", 9)
)

logbox.pack(
    fill="both",
    expand=True
)


# ============================================================
# CLEAN SHUTDOWN
# ============================================================

def close_app():
    global camera_center
    if camera_center is not None:
        try:
            camera_center.close()
        except Exception:
            pass
        camera_center = None
    save_profiles()

    try:
        if client:
            client.loop_stop()
            client.disconnect()
    except Exception:
        pass

    root.destroy()


root.protocol("WM_DELETE_WINDOW", close_app)


# ============================================================
# START
# ============================================================

refresh_device_cards(force=True)
root.after(250, refresh_camera_stream_panel)
root.after(500, auto_start_camera_stream)
root.after(500, poll_recording_service_status)

root.mainloop()
