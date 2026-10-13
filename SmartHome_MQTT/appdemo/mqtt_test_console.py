import cv2
from PIL import Image, ImageTk
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import json
import time
from pathlib import Path
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
            "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        }
    if dtype == "relay":
        return {
            "rooms": [],
            "channels": [
                {"id": 1, "name": "Relay 1", "room": "", "mode": "manual", "default_state": False, "schedule_on": "08:00", "schedule_off": "22:00", "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                {"id": 2, "name": "Relay 2", "room": "", "mode": "manual", "default_state": False, "schedule_on": "08:00", "schedule_off": "22:00", "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                {"id": 3, "name": "Relay 3", "room": "", "mode": "manual", "default_state": False, "schedule_on": "08:00", "schedule_off": "22:00", "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                {"id": 4, "name": "Relay 4", "room": "", "mode": "manual", "default_state": False, "schedule_on": "08:00", "schedule_off": "22:00", "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
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
            base["mode"] = base.get("mode") if base.get("mode") in ("manual", "schedule") else "manual"
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
    dialog.geometry("1100x700" if dtype == "relay" else "560x620")
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

        headers = ["CH", "Tên channel", "Phòng", "Chế độ", "Mặc định", "ON", "OFF", "Ngày chạy"]
        for col, text in enumerate(headers):
            ttk.Label(channel_frame, text=text, font=("Segoe UI", 9, "bold")).grid(row=0, column=col, padx=4, pady=4, sticky="w")

        row_vars = []
        for i, c in enumerate(channels, start=1):
            ttk.Label(channel_frame, text=f"CH{i}").grid(row=i, column=0, padx=4, pady=5, sticky="w")
            nvar = tk.StringVar(value=str(c.get("name") or f"Relay {i}"))
            rvar = tk.StringVar(value=str(c.get("room") or ""))
            mvar = tk.StringVar(value=c.get("mode") if c.get("mode") in ("manual", "schedule") else "manual")
            svar = tk.BooleanVar(value=bool(c.get("default_state", False)))
            onvar = tk.StringVar(value=str(c.get("schedule_on") or "08:00"))
            offvar = tk.StringVar(value=str(c.get("schedule_off") or "22:00"))
            day_vars = {day: tk.BooleanVar(value=day in c.get("schedule_days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])) for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
            ttk.Entry(channel_frame, textvariable=nvar, width=18).grid(row=i, column=1, padx=4, pady=5)
            combo = ttk.Combobox(channel_frame, textvariable=rvar, state="readonly", values=rooms, width=18)
            combo.grid(row=i, column=2, padx=4, pady=5)
            if rvar.get():
                combo.set(rvar.get())
            ttk.Combobox(channel_frame, textvariable=mvar, state="readonly", values=("manual", "schedule"), width=11).grid(row=i, column=3, padx=4, pady=5)
            ttk.Checkbutton(channel_frame, variable=svar).grid(row=i, column=4, padx=4, pady=5)
            ttk.Entry(channel_frame, textvariable=onvar, width=8).grid(row=i, column=5, padx=4, pady=5)
            ttk.Entry(channel_frame, textvariable=offvar, width=8).grid(row=i, column=6, padx=4, pady=5)
            days_frame = ttk.Frame(channel_frame)
            days_frame.grid(row=i, column=7, padx=2, pady=2, sticky="w")
            for j, (day, label) in enumerate(zip(day_vars, ("T2", "T3", "T4", "T5", "T6", "T7", "CN"))):
                ttk.Checkbutton(days_frame, text=label, variable=day_vars[day]).grid(row=0, column=j, padx=1)
            row_vars.append((nvar, rvar, mvar, svar, onvar, offvar, day_vars))

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
            text="manual: điều khiển bằng ON/OFF trên card | schedule: ON tại giờ Schedule ON và OFF tại Schedule OFF | cùng tên phòng có thể gán cho nhiều channel.",
            foreground="gray",
            wraplength=720
        ).pack(anchor="w", padx=15, pady=5)

        def save_edit():
            new_channels = []
            for i, vars_ in enumerate(row_vars, start=1):
                nvar, rvar, mvar, svar, onvar, offvar, day_vars = vars_
                mode_value = mvar.get().strip()
                if mode_value not in ("manual", "schedule"):
                    mode_value = "manual"
                new_channels.append({
                    "id": i,
                    "name": nvar.get().strip() or f"Relay {i}",
                    "room": rvar.get().strip(),
                    "mode": mode_value,
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
# CAMERA STREAM
# ============================================================

camera_capture = None
camera_running = False
camera_thread = None
camera_photo = None


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
    """Refresh the stream controls embedded in the confirmed camera card."""
    if "streamf" not in globals() or "stream_status" not in globals():
        return
    try:
        if not stream_status.winfo_exists():
            return
    except tk.TclError:
        return
    did, profile = get_configured_camera()
    if profile is None:
        if "camera_running" in globals() and camera_running:
            stop_camera_stream()
        if "camera_ip" in globals():
            camera_ip.set("")
        return
    ip = str(profile.get("ip") or "").strip()
    camera_ip.set(ip)
    stream_status.config(text="READY" if ip else "WAITING FOR IP")


def auto_start_camera_stream(device_id=None):
    """Automatically open the stream for a confirmed camera profile."""
    global camera_running, camera_thread
    did, profile = get_configured_camera()
    if profile is None or (device_id and did != device_id):
        return
    if "stream_status" not in globals():
        return
    try:
        if not stream_status.winfo_exists():
            return
    except tk.TclError:
        return
    ip = str(profile.get("ip") or "").strip()
    refresh_camera_stream_panel()
    if not ip or camera_running:
        return
    url = str(profile.get("stream_url") or "").strip()
    if not url:
        url = f"http://{ip}:{int(profile.get('stream_port', 81) or 81)}/stream"
    camera_running = True
    stream_status.config(text="CONNECTING...")
    camera_thread = threading.Thread(target=camera_stream_worker, args=(url,), daemon=True)
    camera_thread.start()


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

# ============================================================
# CAMERA STREAM FUNCTIONS
# ============================================================

def camera_stream_worker(url):

    global camera_capture
    global camera_running
    global camera_photo

    try:
        camera_capture = cv2.VideoCapture(url)

        # Một số ESP32-CAM cần buffer nhỏ
        camera_capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not camera_capture.isOpened():
            log(f"CAMERA STREAM ERROR: Cannot open {url}")
            root.after(
                0,
                lambda: stream_status.config(text="ERROR")
            )
            camera_running = False
            return

        log(f"CAMERA STREAM CONNECTED: {url}")

        root.after(
            0,
            lambda: stream_status.config(text="STREAMING")
        )

        while camera_running:

            ret, frame = camera_capture.read()

            if not ret:
                log("CAMERA STREAM: Frame read failed")
                break

            # Resize để vừa khung GUI
            frame = cv2.resize(
                frame,
                (640, 480),
                interpolation=cv2.INTER_AREA
            )

            # OpenCV BGR -> RGB
            frame = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2RGB
            )

            image = Image.fromarray(frame)

            camera_photo = ImageTk.PhotoImage(image)

            root.after(
                0,
                update_camera_frame,
                camera_photo
            )

    except Exception as e:

        log(f"CAMERA STREAM ERROR: {e}")

    finally:

        if camera_capture:
            camera_capture.release()

        camera_capture = None

        root.after(
            0,
            lambda: stream_status.config(text="OFFLINE")
        )

        log("CAMERA STREAM DISCONNECTED")


def update_camera_frame(photo):

    if camera_running:
        camera_view.config(
            image=photo
        )

        camera_view.image = photo


def start_camera_stream():
    # Compatibility wrapper; normal operation is automatic.
    auto_start_camera_stream()


def stop_camera_stream():
    # Internal stop used when deleting a camera or closing the application.
    global camera_running
    camera_running = False
    if "stream_status" in globals():
        stream_status.config(text="STOPPING...")


def open_camera_status():

    ip = camera_ip.get().strip()

    if not ip:
        for did, profile in profiles.items():
            if profile.get("type") == "camera" and profile.get("ip"):
                ip = str(profile["ip"]).strip()
                camera_ip.set(ip)
                break

    if not ip:
        messagebox.showwarning("Camera", "Chưa nhận được IP của ESP32-CAM qua MQTT.")
        return

    url = ""
    for did, profile in profiles.items():
        if profile.get("type") == "camera" and profile.get("ip") == ip:
            url = profile.get("status_url", "")
            break
    if not url:
        url = f"http://{ip}/status"

    import webbrowser

    webbrowser.open(url)
    
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
            mode_name = c.get("mode") or "manual"
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

        ttk.Label(card, text="Live Stream").grid(row=4, column=0, sticky="w", padx=5, pady=(8, 3))
        ttk.Label(card, text="Camera IP (MQTT)").grid(row=5, column=0, padx=5, pady=5, sticky="e")
        camera_ip = tk.StringVar(value="")
        ttk.Entry(card, textvariable=camera_ip, width=20, state="readonly").grid(row=5, column=1, padx=5, pady=5, sticky="w")
        stream_status = ttk.Label(card, text="OFFLINE")
        stream_status.grid(row=5, column=2, padx=10, sticky="w")
        camera_view = ttk.Label(card, text="ESP32-CAM\nNO VIDEO", anchor="center", justify="center")
        camera_view.grid(row=6, column=0, columnspan=5, padx=5, pady=5)
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
# No START/STOP controls. The stream opens automatically after confirmation.

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
    global camera_running

    camera_running = False

    try:
        if camera_capture:
            camera_capture.release()
    except Exception:
        pass

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

root.mainloop()
