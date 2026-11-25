"""Launch the standalone validation Studio or query its read-only integration."""
import argparse
import json
from pathlib import Path
import sys

from .ai_client import AiClient, Deployment, PROJECT
from .service_monitor import snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--diagnose", action="store_true", help="Verify runtime, model and service health without opening COM.")
    args = parser.parse_args()
    local = PROJECT / "config/studio.local.json"
    path = args.config or (local if local.is_file() else PROJECT / "config/studio.example.json")
    deployment = Deployment.read(path)
    if args.diagnose:
        from .offline_replay import OfflineReplay
        from .serial_transport import available_ports

        replay = OfflineReplay(deployment)
        instance = AiClient(deployment)
        samples = instance.sample()
        scores = samples["xgboost-shadow"]
        print(json.dumps({"services": snapshot(deployment), "capture": deployment.interface,
                          "monitoring": samples["overview"]["monitoring"]["status"],
                          "model": scores["model"], "parameters": replay.model_parameters,
                          "worker": scores["worker"], "parity": scores["parity"],
                          "decision_eligible": scores["decision_eligible"],
                          "enforcement": samples["overview"]["enforcement"], "ports_unopened": available_ports()},
                         indent=2))
        return 0
    from PySide6.QtWidgets import QApplication
    from .ui.window import Window

    app = QApplication([sys.argv[0]])
    window = Window(deployment)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
