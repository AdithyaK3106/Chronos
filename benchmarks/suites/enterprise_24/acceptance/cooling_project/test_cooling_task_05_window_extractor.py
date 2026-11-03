"""cooling_task_05: rolling-window aggregation lives in a reusable WindowFeatureExtractor; features unchanged."""
import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path.cwd() / "src"))
from features import FeatureProcessor, WindowFeatureExtractor  # noqa: E402

# FeatureProcessor.process_dataframe() output at the baseline commit for the frame below.
BASELINE = [[0.1, 0.05, 0.4, 0.5757295649, 0.5651460092, 0.08, 0.1, 0.05, 0.08, 0.1, 0.05, 0.08, 0.5, 0.5, 0.5], [0.35, 0.5, 0.4, 0.6334501945, 0.6280686163, 0.41, 0.225, 0.275, 0.245, 0.225, 0.275, 0.245, 0.625, 0.725, 0.665], [0.8, 0.7, 0.4, 0.6672250705, 0.6648988029, 0.76, 0.4166666667, 0.4166666667, 0.4166666667, 0.4166666667, 0.4166666667, 0.4166666667, 0.725, 0.6, 0.675], [0.95, 0.99, 0.4, 0.6911916341, 0.6910365759, 0.966, 0.55, 0.56, 0.554, 0.55, 0.56, 0.554, 0.575, 0.645, 0.603], [0.2, 0.1, 0.4, 0.7097827643, 0.7113132664, 0.16, 0.48, 0.468, 0.4752, 0.48, 0.468, 0.4752, 0.125, 0.055, 0.097], [0.6, 0.4, 0.4, 0.7249734501, 0.727881895, 0.52, 0.58, 0.538, 0.5632, 0.5, 0.4566666667, 0.4826666667, 0.7, 0.65, 0.68], [0.61, 0.42, 0.4, 0.7378173562, 0.7418912655, 0.534, 0.632, 0.522, 0.588, 0.5157142857, 0.4514285714, 0.49, 0.505, 0.51, 0.507], [0.05, 0.0, 0.4, 0.7489434844, 0.7540272372, 0.03, 0.482, 0.382, 0.442, 0.4575, 0.395, 0.4325, 0.22, 0.29, 0.248], [1.0, 1.0, 0.4, 0.7587575801, 0.7647322612, 1.0, 0.492, 0.384, 0.4488, 0.5177777778, 0.4622222222, 0.4955555556, 0.975, 1.0, 0.985], [0.45, 0.55, 0.4, 0.7675366972, 0.7743084701, 0.49, 0.542, 0.474, 0.5148, 0.511, 0.471, 0.495, 0.225, 0.275, 0.245], [0.3, 0.25, 0.4, 0.7754784547, 0.7829713794, 0.28, 0.482, 0.444, 0.4668, 0.531, 0.491, 0.515, 0.425, 0.35, 0.395], [0.7, 0.65, 0.4, 0.7827287716, 0.7908801274, 0.68, 0.5, 0.49, 0.496, 0.566, 0.506, 0.542, 0.7, 0.7, 0.7], [0.9, 0.85, 0.4, 0.789398463, 0.7981555718, 0.88, 0.67, 0.66, 0.666, 0.576, 0.521, 0.554, 0.6, 0.6, 0.6], [0.15, 0.12, 0.4, 0.7955736696, 0.8048916614, 0.138, 0.5, 0.484, 0.4936, 0.496, 0.434, 0.4712, 0.125, 0.135, 0.129]]


def _frame():
    n = 14
    return pd.DataFrame({
        "cpu": [10, 35, 80, 95, 20, 60, 61, 5, 100, 45, 30, 70, 90, 15],
        "gpu": [5, 50, 70, 99, 10, 40, 42, 0, 100, 55, 25, 65, 85, 12],
        "memory": [40] * n, "disk_io": [1000 * (i + 1) for i in range(n)],
        "network_io": [500 * (i + 1) for i in range(n)], "gpu_temp": [60] * n, "cpu_temp": [55] * n,
    })


def test_extractor_on_its_own():
    w = WindowFeatureExtractor()
    for i in range(1, 13):
        w.push(cpu=i / 100, gpu=0.5, heat=i / 10)
    assert w.mean("cpu", 5) == pytest.approx(sum(range(8, 13)) / 500)
    assert w.mean("heat", 10) == pytest.approx(sum(range(3, 13)) / 100)  # maxlen 10 keeps the last 10
    assert w.mean("gpu", 3) == pytest.approx(0.5)
    w.reset()
    w.push(cpu=0.2, gpu=0.4, heat=0.3)
    assert w.mean("cpu", 5) == pytest.approx(0.2)


def test_custom_channels_and_maxlen():
    w = WindowFeatureExtractor(channels=("temp",), maxlen=3)
    for v in (1.0, 2.0, 3.0, 4.0):
        w.push(temp=v)
    assert w.mean("temp", 10) == pytest.approx(3.0)


def test_feature_processor_uses_it_and_features_are_unchanged():
    fp = FeatureProcessor()
    assert isinstance(fp.windows, WindowFeatureExtractor)
    assert "get_roll" not in inspect.getsource(FeatureProcessor.process_single)
    fp.stats = {"max_disk_log": 12.0, "max_net_log": 11.0}
    np.testing.assert_allclose(fp.process_dataframe(_frame()), np.array(BASELINE), atol=1e-9)
    fp.reset_state()
    np.testing.assert_allclose(fp.process_dataframe(_frame()), np.array(BASELINE), atol=1e-9)
