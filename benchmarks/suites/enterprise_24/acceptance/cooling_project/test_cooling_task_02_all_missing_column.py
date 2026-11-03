"""cooling_task_02: a sensor that never reports must not leave NaN in cleaned data or the saved scaling state."""
import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path.cwd() / "src"))
from preprocess import preprocess_pipeline  # noqa: E402

NUMERIC = ["cpu", "gpu", "memory", "disk_io", "network_io", "gpu_temp", "cpu_temp"]


def _run(tmp_path, monkeypatch, frame):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)  # the pipeline writes to ../data/processed
    frame.to_csv(tmp_path / "in.csv", index=False)
    preprocess_pipeline(str(tmp_path / "in.csv"), str(tmp_path / "out"), str(tmp_path / "state.pkl"))
    with open(tmp_path / "state.pkl", "rb") as fh:
        state = pickle.load(fh)
    return state, pd.read_csv(tmp_path / "out_dataset.csv"), pd.read_csv(tmp_path / "data" / "processed" / "X.csv")


def _frame(n=30, **overrides):
    data = {
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="s").astype(str),
        "cpu": [np.nan if i % 7 == 0 else 40.0 + i for i in range(n)],
        "gpu": [30.0] * n, "memory": [50.0] * n, "disk_io": [2000.0 + i for i in range(n)],
        "network_io": [1000.0 + i for i in range(n)], "gpu_temp": [60.0] * n, "cpu_temp": [55.0] * n,
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_sensor_that_never_reports_leaves_no_nan(tmp_path, monkeypatch):
    state, cleaned, X = _run(tmp_path, monkeypatch, _frame(disk_io=[np.nan] * 30, gpu_temp=[np.nan] * 30))
    assert all(math.isfinite(v) and v > 0 for v in state.values()), state
    assert int(cleaned[NUMERIC].isna().sum().sum()) == 0
    assert int(X.isna().sum().sum()) == 0


def test_partial_gaps_still_use_forward_fill(tmp_path, monkeypatch):
    state, cleaned, _ = _run(tmp_path, monkeypatch, _frame())
    # cpu[0] has no previous value -> imputed; cpu[7] -> forward-filled from cpu[6]
    assert cleaned.loc[7, "cpu"] == 46.0
    assert math.isfinite(cleaned.loc[0, "cpu"])
    assert all(math.isfinite(v) for v in state.values())
