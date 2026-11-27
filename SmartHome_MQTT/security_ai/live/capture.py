"""Helpers for supervising the Wireshark capture tools."""
from pathlib import Path
import re
import subprocess

DUMPCAP = "C:/Program Files/Wireshark/dumpcap.exe"
TSHARK = "C:/Program Files/Wireshark/tshark.exe"


def discover(executable):
    if not Path(executable).is_file():
        raise ValueError("Dumpcap executable missing")
    result = subprocess.run([executable, "-D"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError(f"Interface discovery failed ({result.returncode})")
    return tuple((match[1], match[2] or "") for line in result.stdout.splitlines()
                 if (match := re.fullmatch(r"\d+\. (\S+)(?: \((.*)\))?", line.strip())))


def select(interfaces, selector):
    selector = selector.casefold()
    matches = [name for name, desc in interfaces if
               selector in (name.casefold(), desc.casefold(), name.split("_")[-1].strip("{}").casefold())]
    if len(matches) != 1:
        raise ValueError("Interface selector must uniquely match an identity or description")
    return matches[0]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def counters(log):
    matches = re.findall(r"Packets captured:\s*(\d+)", log, re.I)
    drops = re.findall(r"\bdropped:\s*(\d+)", log, re.I)
    if not drops:
        drops = re.findall(r"received/dropped on interface [^\r\n]+?:\s*\d+/(\d+)", log, re.I)
    return {"packets_captured": int(matches[-1]) if matches else None,
            "packets_dropped": sum(map(int, drops)) if drops else None}
