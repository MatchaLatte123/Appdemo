"""Operator view for the local, authenticated adaptive-security API."""
from datetime import datetime
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import quote

PREFIX = "/api/security/adaptive/"
LIMIT = 4 * 1024 * 1024


def adaptive_request(port, route, data=None):
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state")
    token = (base / "SmartHomeAI/AdaptiveSecurity/operator.token").read_text(encoding="ascii").strip()
    conn = HTTPConnection("127.0.0.1", port, timeout=120 if route == "operator/build" else 4)
    try:
        conn.request("GET" if data is None else "POST", PREFIX + route, body=None if data is None else json.dumps(data),
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Phản hồi V3 vượt giới hạn")
        payload = json.loads(raw)
        if response.status != 200:
            raise ValueError(payload.get("error", "Không đọc được dữ liệu V3"))
        return payload
    finally:
        conn.close()


class AdaptiveSecurityPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.results = SimpleQueue()
        self.port = 8767
        self.pending = set()
        self.summary = ttk.Label(self, text="Đang chờ AI Server V3", wraplength=800)
        self.summary.pack(anchor="w", pady=(0, 8))
        self.readiness = ttk.Label(self, wraplength=830)
        self.readiness.pack(anchor="w", pady=(0, 8))
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.tables = {}
        self.xgboost_data = {}
        layouts = {
            "devices": ("Thiết bị và nguồn", ("Nguồn", "Định danh", "Dịch vụ", "Trạng thái", "Chất lượng")),
            "incidents": ("Sự cố", ("Dịch vụ", "Hành vi", "Mức rủi ro", "Trạng thái")),
            "learning": ("Phiên học", ("Phiên", "Trạng thái", "Mẫu lưu", "Đã loại", "Ghi chú")),
            "profiles": ("Baseline", ("Phiên bản", "Trạng thái", "Hash model")),
            "xgboost-shadow": ("XGBoost · Dự đoán",
                               ("Luồng", "Dự đoán", "Mẫu idle nhận đúng", "Điểm attack", "Trạng thái", "Giờ nhận")),
        }
        for kind, (label, columns) in layouts.items():
            frame = ttk.Frame(tabs, padding=5)
            tabs.add(frame, text=label)
            if kind == "xgboost-shadow":
                self.xgboost_status = ttk.Label(frame, text="Đang chờ điểm XGBoost", wraplength=850)
                self.xgboost_status.pack(anchor="w", pady=(2, 8))
            bar = ttk.Frame(frame)
            bar.pack(fill="x")
            if kind == "learning":
                actions = (("Bắt đầu phiên", self.start_session), ("Dừng", lambda: self.session_action("stop")),
                           ("Duyệt bình thường", lambda: self.session_action("accept")),
                           ("Loại bỏ", lambda: self.session_action("reject")))
            elif kind == "profiles":
                actions = (("Readiness từng dịch vụ", lambda: self.send("profiles")),
                           ("Tạo ứng viên", lambda: self.send("operator/build", {})),
                           ("Kích hoạt", lambda: self.version_action("activate")),
                           ("Quay về bản trước", lambda: self.version_action("rollback")))
            else:
                actions = ()
            for text, handler in actions:
                ttk.Button(bar, text=text, command=handler).pack(side="left", padx=(0, 5))
            ttk.Button(bar, text="Chi tiết", command=lambda k=kind: self.detail(k)).pack(side="right")
            tree = ttk.Treeview(frame, columns=columns, show="headings", height=11, selectmode="browse")
            scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            for title in columns:
                tree.heading(title, text=title)
                tree.column(title, width=130, minwidth=70)
            scroll.pack(side="right", fill="y")
            tree.pack(side="left", fill="both", expand=True)
            tree.bind("<Double-1>", lambda _event, k=kind: self.detail(k))
            self.tables[kind] = tree
        self.timer = self.after(150, self.drain)

    def send(self, route, data=None):
        if route in self.pending:
            return
        self.pending.add(route)

        def worker():
            try:
                self.results.put((route, adaptive_request(self.port, route, data), None))
            except (OSError, HTTPException, ValueError, KeyError) as exc:
                self.results.put((route, None, str(exc)))
        Thread(target=worker, daemon=True).start()

    def refresh(self, port):
        self.port = port
        self.send("overview")
        self.send("devices")
        self.send("incidents")
        self.send("xgboost-shadow")

    def selected(self, kind):
        selection = self.tables[kind].selection()
        return selection[0] if selection else None

    def edit_table(self, kind, rows):
        tree = self.tables[kind]
        selected = self.selected(kind)
        keep = {key for key, _ in rows}
        obsolete = [key for key in tree.get_children() if key not in keep]
        if obsolete:
            tree.delete(*obsolete)
        for i, (key, values) in enumerate(rows):
            if tree.exists(key):
                tree.item(key, values=values)
                tree.move(key, "", i)
            else:
                tree.insert("", i, iid=key, values=values)
        if selected in keep:
            tree.selection_set(selected)

    def render_overview(self, data):
        service = " · ".join(f"{r['service']} {r['service_state']} ({r['quality']})" for r in data["services"])
        self.summary.configure(text=f"{data['baseline']} · {data['learning']} · "
                               f"{data['normal_devices']} thiết bị bình thường · "
                               f"{data['suspicious_devices']} thiết bị bất thường · "
                               f"{data['active_incidents']} sự cố\n"
                               f"{service or data['monitoring']['status']} · "
                               f"Baseline {data['active_version'] or 'chưa kích hoạt'}")
        summary = data.get("learning_summary", {})
        readiness = data.get("service_readiness", {})
        candidate = data["versions"][0]["status"] if data["versions"] else "Chưa tạo ứng viên"
        if summary:
            accepts = summary["sessions"].get("ACCEPTED", 0)
            self.readiness.configure(text=f"{accepts} phiên đã duyệt · {summary['eligible_windows']} mẫu qua kiểm toán · "
                                     f"{summary['excluded_windows']} mẫu bị loại đã ghi · "
                                     f"{summary['duration_seconds'] / 60:.1f} phút · {candidate}\n"
                                     + " · ".join(f"{role} {info['status']}" for role, info in readiness.items())
                                     + (f" · {summary['legacy_unaudited_rows']} mẫu cũ thiếu kiểm toán"
                                        if summary["legacy_unaudited_rows"] else "")
                                     + (" · Có dấu hiệu drift" if any(v["status"] == "POSSIBLE_DRIFT"
                                                                     for v in data.get("drift", {}).values()) else ""))
        else:
            self.readiness.configure(text=f"Đang đọc API V3 trước bản kiểm toán · {candidate}")
        self.edit_table("learning", [
            (r["id"], (r["id"][:12], r["status"], r["samples"], r.get("exclusions", "—") if r.get("audit_complete") else "—", r["note"]))
            for r in data["sessions"]])
        self.edit_table("profiles", [(p["id"], (p["id"][:12], p["status"], (p["model_hash"] or "")[:16]))
                                     for p in data["versions"]])

    def render_devices(self, data):
        rows = []
        for d in data["devices"]:
            if not (identity := d["identity"]):
                continue
            label = identity.get("device_id") or identity.get("candidate_device_id") or identity.get("ip")
            rows.append((d["key"], (label, identity["status"], d["service"], d["detector_state"], d["quality"])))
        self.edit_table("devices", rows)

    def render_incidents(self, data):
        self.edit_table("incidents", [(i["id"], (i["affected_service"]["service"], i["behavior"],
                                                i["severity"], i["status"])) for i in data["incidents"]])

    def render_xgboost(self, data):
        self.xgboost_data = data
        model = data.get("model", {})
        worker = data.get("worker")
        if data.get("status") == "DISABLED":
            heading = "XGBoost chưa được bật trên AI Server."
        elif worker == "ERROR":
            heading = f"XGBoost chưa sẵn sàng · {data.get('reason', 'Lỗi worker')}"
        elif worker == "STARTING":
            heading = "Đang nạp mô hình XGBoost."
        else:
            mode = ("Cột mẫu idle tính trên tập kiểm tra bình thường. Chưa có số đo khả năng phát hiện tấn công tại mạng này."
                    if data.get("status") == "LIVE_PREDICTION" else "Điểm quan sát")
            heading = (f"XGBoost {model.get('run_id', '')[:8]} · {model.get('window_packets', '—')} gói/cửa sổ")
            if "benchmark_threshold" in model:
                heading += f" · Ngưỡng mặc định {model['benchmark_threshold']:.5f}"
            if idle := model.get("idle_thresholds"):
                heading += f" · {len(idle)} luồng dùng ngưỡng idle (xem Chi tiết)"
            heading += f"\n{mode}"
            if data.get("isolation_assessment", {}).get("status") == "UNVALIDATED":
                heading += "\nChưa đủ bằng chứng để tính độ tin cậy cách ly."
        self.xgboost_status.configure(text=heading)
        statuses = {
            "WAITING_FOR_WINDOW": "Chờ đủ gói", "SHADOW_SCORED": "Đã chấm quan sát",
            "PREDICTED": "Đã dự đoán",
            "STALE_WINDOW": "Chờ dữ liệu mới", "CAPTURE_QUALITY_BLOCK": "Lỗi chất lượng thu",
        }
        names = {"MQTT_BROKER": "MQTT Broker", "VIDEO_API": "Video API"}
        rows = []
        for scope, item in data.get("lanes", {}).items():
            score = item["score"]
            idle_check = item.get("idle_validation")
            idle_correct = (f"{idle_check['normal_windows']}/{idle_check['windows']}"
                            if idle_check else "—")
            prediction = {"NORMAL": "Bình thường", "ATTACK": "Tấn công"}.get(item.get("prediction"), "—")
            timestamp = item.get("window", {}).get("end")
            clock = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if timestamp else "—"
            status = statuses.get(item["status"], item["status"])
            if item["status"] == "WAITING_FOR_WINDOW":
                status += f" ({item['pending_packets']}/{item['window_packets']})"
            rows.append((scope, (names.get(scope, scope.replace("CAMERA", "Camera")), prediction,
                                 idle_correct,
                                 f"{score:.5f}" if score is not None else "—", status, clock)))
        self.edit_table("xgboost-shadow", rows)

    def drain(self):
        while True:
            try:
                route, data, error = self.results.get_nowait()
            except Empty:
                break
            self.pending.discard(route)
            if error:
                if route == "overview":
                    self.summary.configure(text="Chưa đọc được V3. Kiểm tra AI Server và quyền operator cục bộ.")
                elif route == "xgboost-shadow":
                    self.xgboost_status.configure(text="Chưa đọc được điểm XGBoost từ AI Server.")
                    self.edit_table("xgboost-shadow", [])
                elif route.startswith("operator/") or "/" in route:
                    messagebox.showerror("AI Security V3", error, parent=self)
            elif route == "overview":
                self.render_overview(data)
            elif route == "devices":
                self.render_devices(data)
            elif route == "incidents":
                self.render_incidents(data)
            elif route == "xgboost-shadow":
                self.render_xgboost(data)
            elif route.startswith("operator/"):
                self.refresh(self.port)
                if route == "operator/build":
                    self.show("Kết quả tạo baseline", data)
            else:
                self.show("Chi tiết AI Security V3", data)
        self.timer = self.after(150, self.drain)

    def show(self, title, data):
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry("760x520")
        scroll = ttk.Scrollbar(window, orient="vertical")
        text = tk.Text(window, wrap="word", yscrollcommand=scroll.set, padx=12, pady=12)
        scroll.configure(command=text.yview)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def detail(self, kind):
        if kind == "xgboost-shadow":
            item = self.selected(kind)
            lane = self.xgboost_data.get("lanes", {}).get(item)
            if lane:
                lane = lane.copy()
                if "confidence" in lane:
                    lane["Khoảng cách chuẩn hóa tới ngưỡng"] = lane.pop("confidence")
                self.show("XGBoost · Chi tiết dự đoán", {
                    "model": self.xgboost_data.get("model"), "parity": self.xgboost_data.get("parity"),
                    "scope": item, "window": lane, "decision_eligible": self.xgboost_data.get("decision_eligible", False),
                    "isolation_assessment": self.xgboost_data.get("isolation_assessment"),
                })
        elif item := self.selected(kind):
            self.send(f"{kind}/{quote(item, safe='')}")

    def start_session(self):
        note = simpledialog.askstring("Phiên học bình thường",
                                      "Ghi bối cảnh hoạt động đã quan sát:", parent=self)
        if note and note.strip():
            self.send("operator/start", {"note": note.strip()})

    def session_action(self, action):
        if not (item := self.selected("learning")):
            return
        fields = {"id": item}
        if action != "stop":
            note = simpledialog.askstring("Duyệt phiên", "Ghi căn cứ duyệt hoặc loại phiên:", parent=self)
            if not note or not note.strip():
                return
            fields["note"] = note.strip()
        self.send(f"operator/{action}", fields)

    def version_action(self, action):
        if item := self.selected("profiles"):
            if messagebox.askyesno("Baseline", "Dùng phiên bản đã chọn để chấm điểm các cửa sổ tiếp theo?",
                                   parent=self):
                self.send(f"operator/{action}", {"id": item})

    def destroy(self):
        self.after_cancel(self.timer)
        super().destroy()
