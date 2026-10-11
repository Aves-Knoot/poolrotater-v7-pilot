"""Local, atomic, last-known-good deployment dwell configuration."""
import json
import math
from pathlib import Path


def deployment_seconds(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 5 <= value <= 3600:
        raise ValueError('deployment_empty_seconds must be a finite number from 5 to 3600')
    return float(value)


def read_settings(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('supervisor settings must not be a symlink')
    if not path.exists():
        return 5.0
    if path.stat().st_size > 8192:
        raise ValueError('supervisor settings must be a small local JSON file')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value)-{'deployment_empty_seconds'}:
        raise ValueError('unknown supervisor settings fields')
    return deployment_seconds(value.get('deployment_empty_seconds',5))
