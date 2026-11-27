"""Load the configured V2 model's preprocessing and display parameters."""
from security_ai.live.xgboost_shadow import load_model, preset_for_order
from security_ai.web_ui.train import verify_run

from .ai_client import ROOT


def load_live_model(deployment):
    if deployment.ai_port is None:
        return None, {}
    settings = deployment.ai_config["xgboost_shadow"]
    order = tuple(settings["feature_order"])
    preset_for_order(order)
    artifact = (ROOT / settings["artifact"]).resolve()
    if not artifact.is_file():
        raise FileNotFoundError(f"Model {settings['run_id'][:8]} is missing: {artifact}")
    folder, report = verify_run(artifact.parents[1], settings["run_id"])
    if (artifact != folder / "model.joblib" or report["model_sha256"] != settings["sha256"]
            or tuple(report["schema"]["order"]) != order):
        raise ValueError("Model artifact, configuration and Studio report disagree")
    model = load_model({**settings, "artifact": artifact})
    parameters = {key: model.named_steps["classifier"].get_params()[key]
                  for key in ("n_estimators", "max_depth", "learning_rate")}
    expected = report["config"]["hyperparameters"][report["algorithm"]]
    if any(expected.get(key) != value for key, value in parameters.items()):
        raise ValueError("Artifact hyperparameters disagree with the training report")
    return model[:-1], parameters
