"""JSON 工具：numpy 标量/数组转原生类型，保证结果可序列化。"""
from __future__ import annotations

import json
import math

import numpy as np


def to_native(obj):
    """递归把 numpy 标量/数组转成 Python 原生类型；NaN/Inf 转 None。"""
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_native(obj.tolist())
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def dumps(obj) -> str:
    return json.dumps(to_native(obj), ensure_ascii=False, separators=(",", ":"))
