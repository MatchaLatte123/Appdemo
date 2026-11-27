"""Launch the Simulation Studio or query its read-only integration."""
import argparse
import json
from pathlib import Path
import sys

from .ai_client import AiClient, Deployment, PROJECT
from .service_monitor import snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--diagnose", action="store_true", help="Verify runtime, model and service health.")
    args = parser.parse_args()
    local = PROJECT / "config/studio.local.json"
    path = args.config or (local if local.is_file() else PROJECT / "config/studio.example.json")
    deployment = Deployment.read(path)
    if args.diagnose:
        from .live_model import load_live_model

        errors = {}
        try:
            _, parameters = load_live_model(deployment)
        except (OSError, ValueError, KeyError) as error:
            errors["model"] = str(error)
            parameters = {}
        instance = AiClient(deployment)
        try:
            samples = instance.sample() if deployment.ai_port is not None else {}
        except (OSError, ValueError) as error:
            errors["api"] = str(error)
            samples = {}
        scores = samples.get("xgboost-shadow", {})
        overview = samples.get("overview", {})
        print(json.dumps({"services": snapshot(deployment), "capture": deployment.interface,
                          "monitoring": overview.get("monitoring", {}).get("status", "AI_DISABLED"),
                          "model": scores.get("model", deployment.ai_config.get("xgboost_shadow")), "parameters": parameters,
                          "worker": scores.get("worker"), "worker_reason": scores.get("reason"),
                          "errors": errors, "parity": scores.get("parity"),
                          "decision_eligible": scores.get("decision_eligible", False),
                          "enforcement": overview.get("enforcement", False)},
                         indent=2))
        return 1 if errors or scores.get("worker") == "ERROR" else 0
    from PySide6.QtWidgets import QApplication
    from .ui.window import Window

    app = QApplication([sys.argv[0]])
    window = Window(deployment)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
