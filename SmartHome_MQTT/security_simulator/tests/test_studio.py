"""Live Studio configuration, feature display and compatibility with saved reports."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from security_ai.web_ui.contract import BASIC_10
from security_simulator.studio.ai_client import AiClient, Deployment, PROJECT
from security_simulator.studio.database import Database
from security_simulator.studio.feature_audit import inspect
from security_simulator.studio.live_model import load_live_model
from security_simulator.studio.report_exporter import export


class SimulatorTest(unittest.TestCase):
    def test_feature_match_and_waiting_do_not_fabricate_prediction(self):
        order = list(BASIC_10)
        key = {"start": 1, "end": 2, "packet_count": 10}
        raw = {"window": key, "features": {name: 0.0 for name in order}, "vector": [0.0] * 10}
        f = {"feature_order": order, "lanes": {"MQTT_BROKER": {"windows": {"10": {"latest": raw}}}}}
        d = {"model": {"feature_order": order, "window_packets": 10, "run_id": "x", "sha256": "y"}, "updated_at": 3,
             "decision_eligible": False, "lanes": {"MQTT_BROKER": {"service": "MQTT", "window": key,
                        "status": "PREDICTED", "prediction": "NORMAL", "score": .2, "threshold": .3}}}
        row = inspect("MQTT_BROKER", d, f)
        self.assertEqual(row["window_age_at_score_seconds"], 1)
        self.assertEqual(row["features"]["IAT"], 0.0)
        changed = key | {"end": 2.1}
        d["lanes"]["MQTT_BROKER"]["window"] = changed
        self.assertIsNone(inspect("MQTT_BROKER", d, f)["features"])
        d["lanes"]["MQTT_BROKER"].update(status="WAITING_FOR_WINDOW", prediction=None, score=None, window=None)
        self.assertIsNone(inspect("MQTT_BROKER", d, f)["prediction"])

    def test_api_get_only_and_threshold_identity(self):
        deployment = Deployment.read(PROJECT / "config/studio.example.json")
        client = AiClient(deployment)
        with self.assertRaisesRegex(ValueError, "read-only"):
            client.get("operator/activate")
        model = deployment.ai_config["xgboost_shadow"].copy()
        client.verify_model({"model": model})
        model["benchmark_threshold"] = .3
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            client.verify_model({"model": model})


    def test_configured_model_loads_from_relocated_artifact(self):
        import hashlib
        import json
        import joblib
        import numpy as np
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline
        from xgboost import XGBClassifier

        with TemporaryDirectory() as temp:
            run_id = "0123456789abcdef0123456789abcdef"
            folder = Path(temp) / "moved workspace" / "runs" / run_id
            folder.mkdir(parents=True)
            artifact = folder / "model.joblib"
            parameters = {"n_estimators": 2, "max_depth": 2, "learning_rate": .1}
            model = Pipeline([("imputer", SimpleImputer()),
                              ("classifier", XGBClassifier(n_jobs=1, **parameters))])
            model.fit(np.arange(40, dtype=np.float32).reshape(4, 10), [0, 0, 1, 1])
            joblib.dump(model, artifact)
            checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
            (folder / "report.json").write_text(json.dumps({
                "run_id": run_id, "model_sha256": checksum, "schema": {"order": BASIC_10},
                "algorithm": "XGBOOST", "config": {"hyperparameters": {"XGBOOST": parameters}},
            }), encoding="utf-8")
            deployment = SimpleNamespace(ai_port=8767, ai_config={"xgboost_shadow": {
                "artifact": str(artifact), "run_id": run_id, "sha256": checksum, "feature_order": BASIC_10,
            }})
            transformer, loaded_parameters = load_live_model(deployment)
            self.assertEqual(transformer.n_features_in_, 10)
            self.assertEqual(loaded_parameters, parameters)
            self.assertEqual(load_live_model(SimpleNamespace(ai_port=None)), (None, {}))

    def test_missing_artifact_names_the_configured_path(self):
        with TemporaryDirectory() as temp:
            model = Path(temp) / "model.joblib"
            deployment = SimpleNamespace(ai_port=8767, ai_config={"xgboost_shadow": {
                "artifact": str(model), "run_id": "0123456789abcdef0123456789abcdef",
                "feature_order": BASIC_10
            }})
            with self.assertRaisesRegex(FileNotFoundError, "Model 01234567 is missing"):
                load_live_model(deployment)

    def test_saved_session_can_still_be_read_and_exported(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            db = Database(root / "records.db")
            session = {"id": "previous-session", "started": 1.0, "ended": 2.0,
                       "model_id": "saved-model", "scope": "MQTT_BROKER",
                       "scenario": "Saved result", "kind": "OFFLINE_REPLAY", "status": "ENDED",
                       "result": {"metrics": {"rows": 1}}}
            db.save(session, [(1.5, "PREDICTION", {"score": 0.1})])
            stored = Database(root / "records.db")
            self.assertEqual(stored.history(), [session])
            report = stored.report(session["id"])
            self.assertEqual(report["observations"][0]["data"], {"score": 0.1})
            files = export(report, root / "out")
            self.assertEqual({p.name for p in files.iterdir()}, {"report.json", "report.html", "observations.csv"})
