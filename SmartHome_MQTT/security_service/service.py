"""Phase 1 Smart Home security telemetry collector. No AI decisions or enforcement."""
from __future__ import annotations
import json, logging, os, sqlite3, threading, time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent

# Tự động tạo thư mục cần thiết
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / "collector.log",
            encoding="utf-8"
        )
    ]
)

CONFIG_PATH = BASE / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
DB_PATH = (BASE / CONFIG["storage"].get("database", "data/security.db")).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "logs/collector.log", encoding="utf-8")])
log = logging.getLogger("security-collector")

state_lock = threading.Lock()
STATE = {"started_at": None, "mqtt": "DISCONNECTED", "network": "STOPPED", "last_mqtt": None,
         "last_packet": None, "packets_total": 0, "mqtt_messages_total": 0, "errors": []}


def utcnow(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS mqtt_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, topic TEXT NOT NULL,
          device_id TEXT, event_type TEXT NOT NULL, payload_json TEXT, payload_size INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_mqtt_ts ON mqtt_events(ts);
        CREATE TABLE IF NOT EXISTS network_windows(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, window_seconds INTEGER NOT NULL,
          scope TEXT NOT NULL, source_ip TEXT, target_ip TEXT, packet_count INTEGER NOT NULL,
          byte_count INTEGER NOT NULL, packets_per_sec REAL NOT NULL, bytes_per_sec REAL NOT NULL,
          tcp_count INTEGER NOT NULL, syn_count INTEGER NOT NULL, rst_count INTEGER NOT NULL,
          udp_count INTEGER NOT NULL, icmp_count INTEGER NOT NULL, unique_dst_ports INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_net_ts ON network_windows(ts);
        CREATE TABLE IF NOT EXISTS devices(
          device_id TEXT PRIMARY KEY, device_type TEXT, ip TEXT, online INTEGER NOT NULL DEFAULT 0,
          last_seen TEXT, last_topic TEXT, last_payload_json TEXT);
        CREATE TABLE IF NOT EXISTS collector_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
          component TEXT NOT NULL, message TEXT NOT NULL);
        ''')
        for item in CONFIG.get("devices", []):
            c.execute("INSERT OR IGNORE INTO devices(device_id,device_type,ip) VALUES(?,?,?)",
                      (item["device_id"], item.get("type"), item.get("ip", "")))


def record_collector_event(level, component, message):
    with db() as c:
        c.execute("INSERT INTO collector_events(ts,level,component,message) VALUES(?,?,?,?)",
                  (utcnow(), level, component, str(message)[:500]))


def extract_device(topic, payload):
    did = payload.get("device_id") or payload.get("id")
    if did: return str(did)
    parts = topic.split("/")
    if len(parts) >= 4 and parts[0:2] == ["smarthome", "device"]:
        key = parts[2].lower()
        return {"relay01":"ESP32-RELAY-01", "fan01":"ESP32-FAN-01", "cam01":"ESP32-CAM-01"}.get(key, key.upper())
    return None


def on_mqtt_message(_client, _userdata, msg):
    raw = bytes(msg.payload)
    try:
        parsed = json.loads(raw.decode("utf-8"))
        safe_payload = json.dumps(parsed, ensure_ascii=False)[:12000] if isinstance(parsed, (dict, list)) else None
        payload = parsed if isinstance(parsed, dict) else {}
    except Exception:
        safe_payload, payload = None, {}
    did = extract_device(msg.topic, payload)
    event_type = msg.topic.rsplit("/", 1)[-1].upper()
    ts = utcnow()
    with db() as c:
        c.execute("INSERT INTO mqtt_events(ts,topic,device_id,event_type,payload_json,payload_size) VALUES(?,?,?,?,?,?)",
                  (ts, msg.topic, did, event_type, safe_payload, len(raw)))
        if did:
            c.execute('''INSERT INTO devices(device_id,online,last_seen,last_topic,last_payload_json)
              VALUES(?,1,?,?,?) ON CONFLICT(device_id) DO UPDATE SET online=1,last_seen=excluded.last_seen,
              last_topic=excluded.last_topic,last_payload_json=excluded.last_payload_json''',
              (did, ts, msg.topic, safe_payload))
    with state_lock:
        STATE["last_mqtt"] = ts
        STATE["mqtt_messages_total"] += 1


class MQTTCollector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.client = None
    def run(self):
        try:
            import paho.mqtt.client as mqtt
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=CONFIG["mqtt"].get("client_id", "SmartHome-AI-Security-Collector"))
            user = CONFIG["mqtt"].get("username", "")
            if user: self.client.username_pw_set(user, CONFIG["mqtt"].get("password", ""))
            self.client.on_message = on_mqtt_message
            def connected(client, _userdata, _flags, reason_code, _properties):
                ok = str(reason_code) in ("Success", "0")
                with state_lock: STATE["mqtt"] = "CONNECTED" if ok else f"ERROR: {reason_code}"
                if ok:
                    for topic in CONFIG["mqtt"].get("topics", []): client.subscribe(topic, qos=0)
                    record_collector_event("INFO", "MQTT", "Connected and subscribed to configured telemetry topics")
            def disconnected(_client, _userdata, _flags, reason_code, _properties):
                with state_lock: STATE["mqtt"] = "DISCONNECTED"
                log.warning("MQTT disconnected: %s", reason_code)
            self.client.on_connect, self.client.on_disconnect = connected, disconnected
            self.client.connect(CONFIG["mqtt"]["host"], int(CONFIG["mqtt"].get("port", 1883)), int(CONFIG["mqtt"].get("keepalive", 60)))
            self.client.loop_forever()
        except Exception as exc:
            with state_lock: STATE["mqtt"] = "ERROR"
            log.exception("MQTT collector failed")
            record_collector_event("ERROR", "MQTT", exc)


class NetworkCollector(threading.Thread):
    """Captures packet metadata only; aggregates per-source and global-target windows."""
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.buckets = defaultdict(lambda: {"packets":0,"bytes":0,"tcp":0,"syn":0,"rst":0,"udp":0,"icmp":0,"ports":set()})
        self.total = 0
    def packet(self, pkt):
        try:
            from scapy.layers.inet import IP, TCP, UDP, ICMP
            from scapy.layers.inet6 import IPv6
            if IP in pkt: src, dst = pkt[IP].src, pkt[IP].dst
            elif IPv6 in pkt: src, dst = pkt[IPv6].src, pkt[IPv6].dst
            else: return
            proto = "tcp" if TCP in pkt else "udp" if UDP in pkt else "icmp" if ICMP in pkt else "other"
            dport = int(pkt[TCP].dport if TCP in pkt else pkt[UDP].dport) if proto in ("tcp", "udp") else None
            flags = str(pkt[TCP].flags) if proto == "tcp" else ""
            now = int(time.time())
            rows = [(f"src:{src}", src, dst), (f"target:{dst}", None, dst)]
            with self.lock:
                for key, source, target in rows:
                    b = self.buckets[(now, key)]
                    b["packets"] += 1; b["bytes"] += len(pkt)
                    b["tcp"] += proto == "tcp"; b["udp"] += proto == "udp"; b["icmp"] += proto == "icmp"
                    b["syn"] += proto == "tcp" and "S" in flags and "A" not in flags
                    b["rst"] += proto == "tcp" and "R" in flags
                    if dport is not None: b["ports"].add(dport)
                self.total += 1
            with state_lock:
                STATE["last_packet"] = utcnow(); STATE["packets_total"] += 1
        except Exception:
            log.debug("Packet parsing skipped", exc_info=True)
    def flush(self):
        now = int(time.time()); window = max(1, int(CONFIG["network"].get("window_seconds", 5)))
        cutoff = now - window
        with self.lock:
            # Sliding window: aggregate all per-second buckets in the last N seconds.
            selected = [(k,v) for k,v in self.buckets.items() if cutoff < k[0] <= now]
            # Expire buckets only after they leave the active window.
            for k in [k for k in self.buckets if k[0] <= cutoff]:
                self.buckets.pop(k, None)
        if not selected: return
        grouped = defaultdict(lambda: {"packets":0,"bytes":0,"tcp":0,"syn":0,"rst":0,"udp":0,"icmp":0,"ports":set()})
        for (sec,key), val in selected:
            g=grouped[key]
            for f in ("packets","bytes","tcp","syn","rst","udp","icmp"): g[f]+=val[f]
            g["ports"].update(val["ports"])
        with db() as c:
            for key,g in grouped.items():
                scope, addr = key.split(":",1)
                source, target = (addr, None) if scope == "src" else (None, addr)
                c.execute('''INSERT INTO network_windows(ts,window_seconds,scope,source_ip,target_ip,packet_count,byte_count,
                  packets_per_sec,bytes_per_sec,tcp_count,syn_count,rst_count,udp_count,icmp_count,unique_dst_ports)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (utcnow(),window,scope,source,target,g["packets"],g["bytes"],g["packets"]/window,g["bytes"]/window,
                   g["tcp"],g["syn"],g["rst"],g["udp"],g["icmp"],len(g["ports"])))
    def run(self):
        if not CONFIG["network"].get("enabled", True):
            with state_lock: STATE["network"] = "DISABLED"
            return
        try:
            from scapy.all import sniff
            iface = CONFIG["network"].get("interface") or None
            with state_lock: STATE["network"] = "CAPTURING"
            record_collector_event("INFO", "NETWORK", f"Capture started; interface={iface or 'default'}")
            def periodic_flush():
                while True:
                    time.sleep(max(1, int(CONFIG["network"].get("flush_interval_seconds",1))))
                    self.flush()
            threading.Thread(target=periodic_flush, daemon=True).start()
            sniff(iface=iface, filter=CONFIG["network"].get("bpf_filter", "ip or ip6"), prn=self.packet, store=False)
        except Exception as exc:
            with state_lock: STATE["network"] = "ERROR"
            log.exception("Network capture failed; run elevated and verify Npcap")
            record_collector_event("ERROR", "NETWORK", exc)


def query_api(path):
    with db() as c:
        if path == "/api/events":
            return [dict(r) for r in c.execute("SELECT * FROM mqtt_events ORDER BY id DESC LIMIT 100")]
        if path == "/api/network":
            return [dict(r) for r in c.execute("SELECT * FROM network_windows ORDER BY id DESC LIMIT 100")]
        if path == "/api/devices":
            return [dict(r) for r in c.execute("SELECT * FROM devices ORDER BY device_id")]
        if path == "/api/summary":
            return {"mqtt_events":c.execute("SELECT COUNT(*) FROM mqtt_events").fetchone()[0],
                    "network_windows":c.execute("SELECT COUNT(*) FROM network_windows").fetchone()[0],
                    "devices":c.execute("SELECT COUNT(*) FROM devices").fetchone()[0]}
        if path == "/api/collector-events":
            return [dict(r) for r in c.execute("SELECT * FROM collector_events ORDER BY id DESC LIMIT 50")]
    return None

HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Smart Home | Security Monitor</title><style>
*{box-sizing:border-box}body{margin:0;background:#101722;color:#e7edf5;font:14px Segoe UI,Arial}header{padding:22px 28px;background:#182435;border-bottom:1px solid #2b3b50}h1{margin:0;font-size:22px}small,.muted{color:#91a2b8}.wrap{padding:22px;max-width:1400px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.card{background:#182435;border:1px solid #2b3b50;border-radius:10px;padding:16px}.value{font-size:22px;font-weight:650;margin-top:8px}.section{margin-top:22px}h2{font-size:16px}table{width:100%;border-collapse:collapse;background:#182435;border-radius:8px;overflow:hidden}th,td{text-align:left;padding:10px;border-bottom:1px solid #2b3b50;font-size:12px}th{color:#a9bbd1;background:#202f43}.pill{padding:4px 8px;border-radius:20px;background:#263b50}.ok{color:#79d6a3}.bad{color:#ff9a9a}button{background:#315d8a;color:white;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}footer{margin:24px 0;color:#91a2b8;font-size:12px}
</style></head><body><header><h1>Smart Home · Security Monitor</h1><small>Giai đoạn 1 — Thu thập và giám sát dữ liệu · AI/Quarantine chưa kích hoạt</small></header><main class="wrap"><div class="grid" id="status"></div><div class="section"><h2>Thiết bị đã quan sát</h2><table><thead><tr><th>Device ID</th><th>Loại</th><th>IP</th><th>Online gần nhất</th><th>Topic cuối</th></tr></thead><tbody id="devices"></tbody></table></div><div class="section"><h2>Network windows gần nhất</h2><table><thead><tr><th>Thời điểm</th><th>Scope</th><th>Source</th><th>Target</th><th>Packets</th><th>Bytes</th><th>Packets/s</th><th>TCP SYN</th><th>Unique ports</th></tr></thead><tbody id="network"></tbody></table></div><div class="section"><h2>MQTT events gần nhất</h2><table><thead><tr><th>Thời điểm</th><th>Device</th><th>Topic</th><th>Event</th><th>Payload bytes</th></tr></thead><tbody id="events"></tbody></table></div><footer>Dữ liệu chỉ phục vụ quan sát và xây dựng dataset. Không có phân loại tấn công, không có hành động cô lập trong phiên bản này. <button onclick="refreshAll()">Làm mới</button></footer></main><script>
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function get(p){let r=await fetch(p);return r.json()}function table(id,rows,cols){document.getElementById(id).innerHTML=rows.map(x=>'<tr>'+cols.map(k=>'<td>'+esc(x[k])+'</td>').join('')+'</tr>').join('')||'<tr><td colspan="9" class="muted">Chưa có dữ liệu</td></tr>'}
async function refreshAll(){try{let [s,sum,d,n,e]=await Promise.all([get('/api/status'),get('/api/summary'),get('/api/devices'),get('/api/network'),get('/api/events')]);document.getElementById('status').innerHTML=[['MQTT',s.mqtt],['Network capture',s.network],['MQTT messages',s.mqtt_messages_total],['Packets captured',s.packets_total],['MQTT records',sum.mqtt_events],['Network windows',sum.network_windows]].map(x=>`<div class="card"><div class="muted">${esc(x[0])}</div><div class="value">${esc(x[1])}</div></div>`).join('');table('devices',d,['device_id','device_type','ip','last_seen','last_topic']);table('network',n,['ts','scope','source_ip','target_ip','packet_count','byte_count','packets_per_sec','syn_count','unique_dst_ports']);table('events',e,['ts','device_id','topic','event_type','payload_size'])}catch(e){console.error(e)}}refreshAll();setInterval(refreshAll,3000);
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        path=urlparse(self.path).path
        if path == "/":
            body=HTML.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
        elif path == "/api/status":
            with state_lock: data=dict(STATE)
            body=json.dumps(data,ensure_ascii=False).encode(); self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        elif path.startswith("/api/"):
            data=query_api(path)
            if data is None: self.send_error(404); return
            body=json.dumps(data,ensure_ascii=False).encode(); self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        else: self.send_error(404); return
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    init_db()
    with state_lock: STATE["started_at"] = utcnow()
    MQTTCollector().start()
    NetworkCollector().start()
    host=CONFIG["server"].get("host","127.0.0.1"); port=int(CONFIG["server"].get("port",8787))
    server=ThreadingHTTPServer((host,port),Handler)
    log.info("Security collector dashboard: http://%s:%s",host,port)
    try: server.serve_forever()
    except KeyboardInterrupt: log.info("Stopping")
    finally: server.server_close()

if __name__ == "__main__": main()
