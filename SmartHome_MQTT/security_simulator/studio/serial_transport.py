"""Bounded newline protocol for the simulator's USB serial port."""
import json
import re
from time import monotonic

import serial
from serial.tools import list_ports

ID = re.compile(r"ESP32-SIM-(?!00)[0-9]{2}$")
PROTOCOL = 1
VERSION = "1.0.0"
MODES = ("IDLE", "NORMAL_MQTT", "NORMAL_HTTP", "NORMAL_RECONNECT", "BOUNDED_PROTOCOL_TEST", "RECOVERY")


def available_ports():
    return [(port.device, port.description) for port in list_ports.comports()]


def parse_reply(line):
    obj = json.loads(line)
    if not isinstance(obj, dict) or obj.get("kind") != "SMART_HOME_SIM":
        raise ValueError("Not a simulator response")
    if obj.get("protocol") != PROTOCOL or obj.get("version") != VERSION:
        raise RuntimeError("Simulator firmware version mismatch")
    if (not isinstance(obj.get("device_id"), str) or (obj["device_id"] and not ID.fullmatch(obj["device_id"]))
            or not re.fullmatch(r"[0-9a-f]{8}", str(obj.get("boot_id")))
            or obj.get("mode") not in MODES or type(obj.get("wifi")) is not bool):
        raise ValueError("Malformed simulator response")
    return obj


class SerialLink:
    def __init__(self, port, serial_factory=serial.Serial):
        self.transport = serial_factory(port=None, baudrate=115200, timeout=0.1, write_timeout=0.5)
        self.transport.dtr = False
        self.transport.rts = False
        self.transport.port = port
        self.transport.open()

    def request(self, command, *, timeout=0.9):
        if (command not in ("HELLO", "STATUS", "STOP", "RESET")
                and not (command.startswith("SET_ID ") and ID.fullmatch(command[7:]))
                and command not in ("START " + mode for mode in MODES[1:])):
            raise ValueError("Unsupported serial command")
        self.transport.reset_input_buffer()
        self.transport.write((command + "\n").encode("ascii"))
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            line = self.transport.read_until(b"\n", size=512)
            if not line.endswith(b"\n"):
                continue
            try:
                return parse_reply(line)
            except (ValueError, UnicodeError):
                continue
        raise TimeoutError(f"Simulator did not respond to {command.split()[0]}")

    def close(self):
        self.transport.close()
