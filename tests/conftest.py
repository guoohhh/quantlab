import pytest

from quantlab.config import Settings


@pytest.fixture
def settings(tmp_path):
    values = {
        "system": {"database_path": "quantlab.db", "data_dir": "data"},
        "calibration": {"flat_threshold_pct": 1.0},
    }
    return Settings(values=values, root=tmp_path)
