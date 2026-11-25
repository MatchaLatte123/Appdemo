"""Launch the Video and AI servers from a single external JSON configuration."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def path_from(value):
    return (ROOT / os.path.expandvars(value)).resolve()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass
class Service:
    name: str
    bind: str
    port: int
    route: str
    command: list
    env: dict

    @property
    def probe_host(self):
        return "127.0.0.1" if self.bind == "0.0.0.0" else self.bind

    def probe(self):
        connection = HTTPConnection(self.probe_host, self.port, timeout=1)
        try:
            connection.request("GET", self.route)
            response = connection.getresponse()
            if response.status != 200:
                return None
            data = json.loads(response.read(256 * 1024))
        except (OSError, HTTPException, ValueError):
            return None
        finally:
            connection.close()
        if not isinstance(data, dict):
            return None
        if self.name == "ai":
            return data if data.get("mode") == "BEHAVIORAL_V3" else None
        return data if all(key in data for key in ("service", "cameras", "ffmpeg_available")) else None

    def verify_port_available(self):
        with socket.socket() as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind((self.bind, self.port))
            except OSError as exc:
                raise RuntimeError(f"{self.name}: cannot bind {self.bind}:{self.port}. Check the IP or port owner.") from exc


def checked_port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("Invalid server port")
    return value


def get_services(settings):
    video, ai = settings["video"], settings["ai"]
    video_config, ai_config = path_from(video["config"]), path_from(ai["config"])
    video_settings = read_json(video_config)
    read_json(ai_config)
    video_port = checked_port(video_settings.get("http_port", 8765))
    video_bind = str(IPv4Address(video_settings.get("http_host", "127.0.0.1")))
    ai_port = checked_port(ai.get("port", 8767))
    if video_port == ai_port and video_bind in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("The configured AI and Video endpoints conflict")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    bin_paths = [str(path_from(p)) for p in video.get("path_entries", [])]
    video_env = {**env, "PATH": os.pathsep.join([*bin_paths, env.get("PATH", "")])}
    video_python, ai_python = path_from(video["python"]), path_from(ai["python"])
    for path in (video_python, ai_python):
        if not path.is_file():
            raise FileNotFoundError(f"Python executable not found: {path}")
    ai_command = [str(ai_python), "-B", "-m", "security_ai_v3.ai_server", "--config", str(ai_config),
                  "--port", str(ai_port)]
    live = ai["live"]
    ai_command += ["--live-continuous", "--live-interface", live["interface"],
                   "--live-host", str(IPv4Address(live["host"])), "--live-video-port", str(video_port),
                   "--live-video-config", str(video_config)]
    if live.get("isolate_decoders") is True:
        ai_command.append("--isolated-capture")
        if live.get("capture_budget"):
            ai_command += ["--capture-budget", str(path_from(live["capture_budget"]))]

    services = [
        Service("video", video_bind, video_port, "/health",
                [str(video_python), "-B", "-m", "recording_service", "--config", str(video_config)], video_env),
        Service("ai", "127.0.0.1", ai_port, "/api/security/dashboard", ai_command, env),
    ]
    return services, video_settings


def verify_video_tools(service, settings):
    for tool in ("ffmpeg", "ffprobe"):
        executable = settings.get(tool + "_path", tool)
        if not shutil.which(executable, path=service.env["PATH"]):
            raise FileNotFoundError(f"{tool} not found. Set video.path_entries in the launcher JSON.")


@contextmanager
def launch_lock(logs):
    import msvcrt

    with (logs / "launcher.lock").open("a+b") as file:
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another server launch is already in progress") from exc
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def launch(services, video_settings, logs, timeout):
    logs.mkdir(parents=True, exist_ok=True)
    with launch_lock(logs):
        pending = []
        for service in services:
            if service.probe() is not None:
                print(f"{service.name.upper()} already running at {service.probe_host}:{service.port}", flush=True)
                continue
            service.verify_port_available()
            if service.name == "video":
                verify_video_tools(service, video_settings)
            pending.append(service)
        children = []
        for service in pending:
            log_path = logs / f"{service.name}.startup.log"
            with log_path.open("ab") as stream:
                process = subprocess.Popen(service.command, cwd=ROOT, env=service.env,
                                           stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            children.append((service, process, log_path))
        deadline = time.monotonic() + timeout
        failures = []
        while children and time.monotonic() < deadline:
            for item in children[:]:
                service, process, log_path = item
                if service.probe() is not None:
                    print(f"{service.name.upper()} ready at {service.probe_host}:{service.port}", flush=True)
                elif process.poll() is None:
                    continue
                else:
                    failures.append(f"{service.name} exited ({process.returncode}); see {log_path}")
                children.remove(item)
            if children:
                time.sleep(.25)
        failures += [f"{service.name} startup not confirmed; see {log_path}"
                     for service, _, log_path in children]
        if failures:
            raise RuntimeError("\n".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "servers.launch.json")
    parser.add_argument("--check", action="store_true", help="Validate config and tools without starting processes")
    args = parser.parse_args()
    try:
        settings = read_json(args.config)
        services, video_settings = get_services(settings)
        timeout = settings.get("startup_timeout_seconds", 45)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("startup_timeout_seconds must be an integer from 1 to 300")
        if args.check:
            verify_video_tools(services[0], video_settings)
            for service in services:
                print(subprocess.list2cmdline(service.command))
            return 0
        launch(services, video_settings, path_from(settings["logs_dir"]), timeout)
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
