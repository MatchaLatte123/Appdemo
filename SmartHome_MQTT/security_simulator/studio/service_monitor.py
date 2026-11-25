"""One lightweight read per service when a sample is requested."""
from http.client import HTTPConnection
import socket


def snapshot(deployment):
    status = {}
    try:
        with socket.create_connection((deployment.host, deployment.broker_port), timeout=1):
            status["mqtt_tcp"] = "OPEN"
    except OSError:
        status["mqtt_tcp"] = "UNAVAILABLE"
    conn = HTTPConnection(deployment.video_host, deployment.video_port, timeout=1)
    try:
        conn.request("GET", "/health")
        status["video_http"] = conn.getresponse().status
    except OSError:
        status["video_http"] = "UNAVAILABLE"
    finally:
        conn.close()
    status["probe_origin"] = "STUDIO_HOST"
    return status
