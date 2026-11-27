"""Project the Ethernet collector onto the CIC_38 training feature contract."""
from ctypes import c_float
from hashlib import sha256
import json
from math import isfinite

from ..web_ui.cic_full import CIC_38

SCHEMA = "cic38-author-aligned-v1"
WINDOW_PACKETS = 10


class Cic38:
    def __init__(self, dataset):
        manifest_bytes = (dataset / "manifest.json").read_bytes()
        preprocessing_bytes = (dataset / "preprocessing.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        preprocessing = json.loads(preprocessing_bytes)
        if (tuple(manifest["features"]) != CIC_38 or tuple(preprocessing["features"]) != CIC_38
                or preprocessing["method"] != "z-score" or preprocessing["fit_on"] != "TRAIN"):
            raise ValueError(f"Invalid CIC_38 feature order or preprocessing metadata: {dataset}")
        parameters = preprocessing["parameters"]
        if set(parameters) != set(CIC_38):
            raise ValueError(f"CIC_38 scaler parameters are incomplete: {dataset}")
        self.scales = tuple((float(parameters[name]["mean"]), float(parameters[name]["scale"])) for name in CIC_38)
        if any(not isfinite(mean) or not isfinite(scale) or scale <= 0 for mean, scale in self.scales):
            raise ValueError(f"Invalid CIC_38 normalization parameters: {dataset}")
        self.dataset_sha = sha256(manifest_bytes).hexdigest()
        self.scaler_sha = sha256(preprocessing_bytes).hexdigest()

    def project(self, snapshot):
        windows = {}
        for size, state in snapshot["windows"].items():
            newest = state["latest"]
            if newest is not None:
                raw = {name: newest["features"][name] for name in CIC_38}
                # Training stores cleaned values in Float32, then computes z-score in Float64.
                vector = tuple((c_float(raw[name]).value - mean) / scale
                               for name, (mean, scale) in zip(CIC_38, self.scales, strict=True))
                newest = {**newest, "features": raw, "normalized_vector": vector}
            windows[size] = {**state, "latest": newest}
        return windows

    def metadata(self):
        return {"schema": SCHEMA, "feature_order": CIC_38,
                "dataset_manifest_sha256": self.dataset_sha, "preprocessing_sha256": self.scaler_sha,
                "normalization": "TRAIN_Z_SCORE", "decision_eligible": False}
