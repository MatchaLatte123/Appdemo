"""Read-only packet capture view fed by the live Studio poll."""
from collections import deque
from time import monotonic

from PySide6 import QtWidgets as Qt
import pyqtgraph as pg

from .presentation import attack_score, card, label, lane_name, status


class CaptureDashboard(Qt.QWidget):
    HISTORY_SECONDS = 90

    def __init__(self, scopes, table, fill):
        super().__init__()
        self.scopes = scopes
        self.fill = fill
        self.previous = None
        self.history = deque(maxlen=220)
        layout = Qt.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        metrics = Qt.QHBoxLayout()
        values = []
        for title in ("Bộ thu Ethernet", "Gói đã bắt", "Tốc độ bắt gói", "Cửa sổ hoàn thành"):
            widget, value = card(title)
            metrics.addWidget(widget, 1)
            values.append(value)
        self.capture_state, self.frames, self.rate, self.completed = values
        layout.addLayout(metrics)
        self.summary = label("Đang chờ dữ liệu bộ thu.", "muted")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.chart = pg.PlotWidget(background="#ffffff")
        self.chart.setMinimumHeight(180)
        self.chart.setMouseEnabled(x=False, y=False)
        self.chart.setLabel("left", "Gói/s", color="#36556a")
        self.chart.setLabel("bottom", "Giây so với lần cập nhật gần nhất", color="#36556a")
        self.chart.showGrid(x=True, y=True, alpha=.15)
        self.chart.setXRange(-self.HISTORY_SECONDS, 0, padding=0)
        self.chart.setYRange(0, 1, padding=0)
        self.chart.addLegend(offset=(12, 10), labelTextColor="#274b60")
        self.curve = self.chart.plot(pen=pg.mkPen("#08737b", width=2.5), name="Tổng gói IPv4/TCP",
                                     symbol="o", symbolSize=4, symbolBrush="#08737b", symbolPen=None)
        layout.addWidget(self.chart, 2)

        self.table = table(("Luồng", "Gói bắt", "Dữ liệu", "Đang gom",
                            "Cửa sổ", "Gói bỏ", "Attack %", "Ngưỡng", "Dự đoán"))
        self.table.setStyleSheet("QTableWidget, QHeaderView::section { font-size: 12px; }")
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(Qt.QHeaderView.ResizeMode.Stretch)
        for column in (6, 7, 8):
            header.setSectionResizeMode(column, Qt.QHeaderView.ResizeMode.ResizeToContents)
        for column, description in {
            4: "Số cửa sổ đã hoàn thành", 5: "Số gói trong các cửa sổ bị bỏ",
            6: "Điểm attack đầu ra của mô hình, hiển thị theo phần trăm.",
            7: "Điểm bằng hoặc cao hơn ngưỡng được phân loại là tấn công.",
        }.items():
            self.table.horizontalHeaderItem(column).setToolTip(description)
        layout.addWidget(self.table, 1)
        self.detail = label("", "muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

    def unavailable(self):
        self.previous = None
        self.history.clear()
        self.curve.setData([], [])
        self.chart.setYRange(0, 1, padding=0)
        for value in (self.frames, self.rate, self.completed):
            value.setText("—")
        self.capture_state.setText("Mất kết nối")
        self.summary.setText("Chưa đọc được bộ thu từ AI Server.")
        self.detail.clear()
        self.table.setRowCount(0)

    def update_snapshot(self, monitoring, features, prediction, *, now=None):
        now = monotonic() if now is None else now
        counters = monitoring.get("services", {})
        total = sum(row.get("frames", 0) for row in counters.values())
        state = monitoring.get("capture_status", monitoring.get("status"))
        self.capture_state.setText(status(state))
        self.frames.setText(f"{total:,}")
        current = None
        if self.previous:
            then, count = self.previous
            if total < count:
                self.history.clear()
            elif now > then:
                current = (total - count) / (now - then)
                self.history.append((now, current))
        self.previous = now, total
        while self.history and now - self.history[0][0] > self.HISTORY_SECONDS:
            self.history.popleft()
        rates = [rate for _, rate in self.history]
        self.curve.setData([stamp - now for stamp, _ in self.history], rates)
        self.chart.setYRange(0, max(1, max(rates, default=0) * 1.15), padding=0)
        self.rate.setText("—" if current is None else f"{current:,.1f} gói/s")

        model = prediction["model"]
        size = model["window_packets"]
        lanes = prediction["lanes"]
        packet_lanes = monitoring.get("packet_lanes", {})
        rows = []
        completed = 0
        for scope in self.scopes:
            windows = features.get("lanes", {}).get(scope, {}).get("windows", {})
            window = windows.get(str(size), windows.get(size, {}))
            traffic = packet_lanes.get(scope, {})
            frames, byte_count = traffic.get("frames"), traffic.get("bytes")
            ready = window.get("completed")
            completed += ready or 0
            result = lanes.get(scope, {})
            rows.append((lane_name(scope), "—" if frames is None else f"{frames:,}",
                         "—" if byte_count is None else f"{byte_count / 1024**2:,.2f} MiB",
                         f"{window['pending']} / {size}" if "pending" in window else "—",
                         ready if ready is not None else "—", window.get("discarded", "—"),
                         attack_score(result.get("score")), attack_score(result.get("threshold")),
                         status(result.get("prediction") or result.get("status"))))
        self.fill(self.table, rows)
        self.completed.setText(f"{completed:,}")
        self.summary.setText(
            f"{monitoring.get('interface', '—')} · {monitoring.get('host', '—')} · "
            f"{size} gói/cửa sổ · XGBoost {model['run_id'][:12]} · {status(prediction['worker'])}")
        last_seen = max((row["last_seen"] for row in counters.values() if row.get("last_seen")), default=None)
        drops = monitoring.get("packet_drops")
        dropped = "chưa có số liệu" if drops is None else f"{drops:,}"
        self.detail.setText(
            f"Lần bắt gần nhất {last_seen or '—'} · Gói mất theo bộ thu {dropped} · "
            f"Khởi động lại {monitoring.get('restarts', 0)} · Dòng giải mã lỗi {monitoring.get('invalid_packets', 0)}")
