"""Styling and display formatting shared by the Studio screens."""
from PySide6 import QtWidgets as Qt

STYLE = """
QWidget { font: 14px "Segoe UI"; color: #172b3e; }
QMainWindow, QWidget#workspace { background: #edf3f8; }
QWidget#sidebar { background: #132a3b; }
QLabel#brand { color: #effbff; font-size: 20px; font-weight: 700; }
QLabel#brandCaption { color: #98bccb; font-size: 14px; }
QLabel#pageTitle { color: #122b40; font-size: 30px; font-weight: 700; }
QLabel#sectionTitle { color: #122b40; font-size: 16px; font-weight: 650; }
QLabel#muted { color: #536d81; }
QListWidget#navigation { background: transparent; border: 0; outline: 0; }
QListWidget#navigation::item { color: #c3d9e3; min-height: 34px; padding: 3px 12px; margin: 2px; }
QListWidget#navigation::item:hover { background: #234457; border-radius: 7px; }
QListWidget#navigation::item:selected {
    background: #285368; color: #ffffff; border-left: 3px solid #61d8c6; border-radius: 7px;
}
QFrame#card { background: #ffffff; border: 1px solid #d5e0e9; border-radius: 10px; }
QLabel#metricLabel { color: #536d81; font-size: 12px; }
QLabel#metricValue { color: #15384b; font-size: 20px; font-weight: 700; }
QPushButton { background: #fff; border: 1px solid #c2d2de; border-radius: 6px; padding: 7px 11px; }
QPushButton:hover { background: #edf7f9; border-color: #85afba; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QSpinBox:focus { border-color: #087c82; }
QPushButton[role="primary"] { background: #08737b; border-color: #08737b; color: #fff; font-weight: 600; }
QPushButton[role="primary"]:hover { background: #075e65; }
QPushButton[role="danger"] { color: #a22438; border-color: #e5a6b0; }
QPushButton[role="danger"]:hover { background: #fff0f2; }
QPushButton:disabled { color: #687c8a; background: #e5edf2; border-color: #d4e0e9; }
QLineEdit, QSpinBox, QComboBox { background: #fff; border: 1px solid #c7d6e0; border-radius: 5px; padding: 6px 8px; }
QComboBox::drop-down { border: 0; width: 22px; }
QPlainTextEdit, QTableWidget { background: #fff; border: 1px solid #d4e0e9; border-radius: 8px; }
QPlainTextEdit { padding: 8px; font: 12px "Consolas"; }
QTableWidget { gridline-color: #dee8ee; alternate-background-color: #f4f8fb; selection-background-color: #cfe9eb;
               selection-color: #143947; }
QHeaderView::section { background: #e4eef5; color: #274b60; border: 0; padding: 8px; font-weight: 600; }
QStatusBar { background: #e2ebf2; color: #36556a; }
"""

LANE_STATUS = {
    "NO_INBOUND_EVIDENCE": "Chưa thấy gói vào",
    "NORMAL": "Bình thường", "ATTACK": "Tấn công", "PREDICTED": "Đã dự đoán",
    "WAITING_FOR_WINDOW": "Đang chờ đủ gói", "WAITING_FOR_PACKETS": "Đang chờ đủ gói",
    "STALE_WINDOW": "Cửa sổ đã cũ", "CAPTURE_QUALITY_BLOCK": "Đầu vào chưa đạt",
    "SHADOW_SCORED": "Đã chấm quan sát", "MONITORING": "Đang giám sát",
    "WAITING_FOR_TRAFFIC": "Chờ lưu lượng", "RETRYING": "Đang kết nối lại",
    "CAPTURE_ERROR": "Lỗi bắt gói", "ENDED": "Đã dừng",
    "READY": "Sẵn sàng", "STARTING": "Đang nạp", "ERROR": "Lỗi",
    "OPEN": "Cổng mở", "UNAVAILABLE": "Không phản hồi",
    "ACTIVE": "Đang chạy", "VERIFIED_OFFLINE_EXECUTION": "Đã chạy offline",
    "TIME_LIMIT": "Hết thời gian", "OPERATOR_CLOSED": "Đã kết thúc",
    "STOP_REQUESTED": "Đã gửi lệnh dừng", "PARTICIPANT_LOST": "Mất kết nối board",
    "OBSERVATION_INTERRUPTED": "Gián đoạn quan sát", "STUDIO_CLOSED": "Đã đóng Studio",
}


def status(value):
    return LANE_STATUS.get(value, value) if value is not None else "—"


def lane_name(scope):
    if scope == "ETHERNET_IPV4_ARP":
        return "Ethernet IPv4/ARP"
    if scope.startswith("CAMERA["):
        return "Camera " + scope.removeprefix("CAMERA[").replace("]:", ":", 1)
    return {"MQTT_BROKER": "MQTT Broker", "VIDEO_API": "Video API"}.get(scope, scope)


def number(value, digits=5):
    return "—" if value is None else f"{value:.{digits}g}"


def percent(value):
    return "—" if value is None else f"{value:.2%}"


def attack_score(value):
    if value is None:
        return "—"
    if 0 < value < 1e-8:
        return "< 0.000001%"
    return f"{value * 100:.6f}".rstrip("0").rstrip(".") + "%"


def label(text, name):
    widget = Qt.QLabel(text, objectName=name)
    return widget


def card(title, detail="—"):
    box = Qt.QFrame(objectName="card")
    layout = Qt.QVBoxLayout(box)
    layout.setContentsMargins(13, 10, 13, 10)
    layout.setSpacing(5)
    layout.addWidget(label(title, "metricLabel"))
    value = label(detail, "metricValue")
    value.setWordWrap(True)
    layout.addWidget(value)
    return box, value
