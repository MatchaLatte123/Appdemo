from __future__ import annotations

import json
import logging
from queue import Queue

import paho.mqtt.client as mqtt


class CameraStatusListener:
    """Small MQTT adapter; callbacks only validate and enqueue status updates."""

    def __init__(self, broker: str, port: int, topic: str, events: Queue):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.events = events
        self.log = logging.getLogger("RECORDER")
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="SmartHome-Video-Recorder",
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self.log.error("MQTT connection failed: %s", reason_code)
            return
        self.log.info("MQTT connected to %s:%s", self.broker, self.port)
        client.subscribe(self.topic, qos=1)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            device_id = str(payload.get("device_id") or "").strip()
            stream_url = str(payload.get("stream") or "").strip()
            ip = str(payload.get("ip") or "").strip()
            if device_id and stream_url:
                self.events.put({
                    "device_id": device_id,
                    "stream_url": stream_url,
                    "ip": ip,
                    "online": payload.get("online", True) is not False,
                })
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            self.log.warning("Ignoring invalid camera MQTT status: %s", exc)

    def start(self) -> None:
        self.client.connect_async(self.broker, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            self.log.debug("MQTT disconnect during shutdown failed", exc_info=True)

