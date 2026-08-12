"""Put the `python/` directory on sys.path so scripts can import ecg_ref directly."""

import pathlib
import sys

_PYTHON_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))
