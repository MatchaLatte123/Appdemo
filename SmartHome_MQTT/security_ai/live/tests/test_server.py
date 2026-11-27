from contextlib import closing
from http.client import HTTPConnection
import json
from threading import Thread

from security_ai.live.server import API, MODE, make_server


def test_public_health_and_operator_scope():
    class Capture:
        def snapshot(self):
            return {"status": "ACTIVE"}

    class Shadow:
        def snapshot(self):
            return {"worker": "READY"}

    class Runtime:
        monitor = Capture()
        shadow = Shadow()

        def get(self, resource):
            if resource != "overview":
                raise KeyError(resource)
            return {"enforcement": False, "mode": MODE}

    token = "unit-test-token"
    with make_server(0, Runtime(), token) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def get(path, headers=None):
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as conn:
                    conn.request("GET", path, headers=headers or {})
                    response = conn.getresponse()
                    return response.status, json.load(response)

            assert get("/health") == (200, {"mode": MODE, "worker": "READY", "capture": "ACTIVE"})
            assert get(API + "overview")[0] == 401
            auth = {"Authorization": f"Bearer {token}"}
            assert get(API + "overview", auth) == (200, {"enforcement": False, "mode": MODE})
            assert get(API + "overview", {**auth, "Origin": "http://example.test"})[0] == 401
            assert get(API + "unknown", auth)[0] == 404
        finally:
            server.shutdown()
            thread.join(timeout=3)
