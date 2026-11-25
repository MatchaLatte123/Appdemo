"""Keep each simulator tied to its serial connection and boot identity."""
from datetime import datetime, timezone

from .serial_transport import ID, SerialLink


class DeviceManager:
    def __init__(self, link_factory=SerialLink):
        self.link_factory = link_factory
        self.links = {}
        self.states = {}

    def connect(self, port):
        if port in self.links:
            raise ValueError("That COM port is already connected")
        link = self.link_factory(port)
        try:
            try:
                data = link.request("HELLO", timeout=1.2)
            except TimeoutError:
                data = link.request("HELLO", timeout=1.6)
        except Exception:
            link.close()
            raise
        self.links[port] = link
        return self.record(port, data)

    def record(self, port, data):
        prior = self.states.get(port, {})
        state = {**data, "port": port, "online": True, "last_seen": datetime.now(timezone.utc).isoformat(),
                 "rebooted": prior.get("rebooted", False) or (
                     prior.get("boot_id") is not None and prior["boot_id"] != data["boot_id"])}
        self.states[port] = state
        return state

    def command(self, port, command):
        try:
            return self.record(port, self.links[port].request(command))
        except (OSError, RuntimeError, TimeoutError):
            self.disconnect(port, stop=False)
            raise

    def configure_id(self, port, device_id):
        if not ID.fullmatch(device_id) or any(s["online"] and s["device_id"] == device_id and p != port
                                             for p, s in self.states.items()):
            raise ValueError("Invalid or duplicate simulator ID")
        state = self.command(port, "SET_ID " + device_id)
        if state["result"] != "OK":
            raise ValueError("Firmware rejected ID assignment")
        return state

    def poll(self):
        for port in tuple(self.links):
            try:
                self.command(port, "STATUS")
            except (OSError, RuntimeError, TimeoutError):
                continue
        return [state.copy() for state in self.states.values()]

    def participants(self, ports):
        states = [self.states[p] for p in ports]
        ids = [s["device_id"] for s in states]
        if any(not s["online"] or s["rebooted"] or not s["wifi"] for s in states):
            raise ValueError("Connect and confirm current Wi-Fi status for all selected simulators")
        if any(not x for x in ids) or len(set(ids)) != len(ids):
            raise ValueError("Selected boards need unique NVS identities")
        for device_id in ids:
            if sum(s["online"] and s["device_id"] == device_id for s in self.states.values()) != 1:
                raise ValueError(f"Duplicate identity: {device_id}")
        return [s.copy() for s in states]

    def stop_all(self):
        errors = []
        for port in tuple(self.links):
            try:
                self.command(port, "STOP")
            except (OSError, RuntimeError, TimeoutError):
                errors.append(port)
        return errors

    def disconnect(self, port, *, stop=True):
        link = self.links.pop(port, None)
        if link is not None:
            try:
                if stop:
                    link.request("STOP")
            except (OSError, RuntimeError, TimeoutError):
                pass
            finally:
                link.close()
        if port in self.states:
            self.states[port] = {**self.states[port], "online": False, "rebooted": False}

    def close(self):
        for port in tuple(self.links):
            self.disconnect(port)
