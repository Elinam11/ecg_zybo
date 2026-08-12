import pathlib
import sys

_PYTHON_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

import pytest  # noqa: E402

from ecg_ref import config  # noqa: E402


def record_available(record: str = "100") -> bool:
    return (config.MITDB_DIR / f"{record}.hea").exists()


requires_data = pytest.mark.skipif(not record_available(),
                                   reason="MIT-BIH data not downloaded")
