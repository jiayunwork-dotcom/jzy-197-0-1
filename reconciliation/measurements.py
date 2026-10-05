"""测量模型：测量值、相对标准差（RSD）默认值与数据集校验。

默认 RSD（相对标准差，1σ 相对误差）的确定
-----------------------------------------
取自选矿厂流程考查中常见仪表/化验手段的经验量级，可被每个测量值显式覆盖，
也可在提交校正时通过 ``default_rsd`` 整体覆盖：

============================  =====  ==========================================
类别                          默认   依据
============================  =====  ==========================================
flow（皮带秤/流量计干矿量）   2.0%   电子皮带秤 1%~3%，矿浆流量计+浓度反算
                                     干矿量误差更大，取 2% 代表一般工况
grade（化验品位）             5.0%   X 荧光/化学分析相对误差约 3%~8%（低品位
                                     样品相对误差更高），取 5%
============================  =====  ==========================================

绝对标准差按 ``sigma = max(rsd * |测量值|, 绝对下限)`` 计算。绝对下限保证
测量值接近 0 时权重不会发散，也保证“大方差吸收调整”等性质连续成立：

* 流量：``sigma_F >= 1e-6 * 最大已测流量``（量纲自适应，随回路同比缩放）；
* 品位：``sigma_g >= 1e-4``（百分点，即 1 ppm 绝对品位）。

稳定测量 id
-----------
测量不要求客户端给 id。未给出时由 ``(物流id, 类型, 元素)`` 内容生成
确定性 id，这样“补送/更正形成新版本”以及“剔除测量”在不同版本之间
能稳定地指向同一个测量点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

MeasurementType = Literal["flow", "grade"]

DEFAULT_RSD: dict[str, float] = {
    "flow": 0.02,
    "grade": 0.05,
}

# 绝对 sigma 下限
FLOW_SIGMA_FLOOR_FRACTION = 1e-6
GRADE_SIGMA_FLOOR = 1e-4  # 百分点


def measurement_id(stream_id: str, mtype: str, element: str | None = None) -> str:
    """由内容生成确定性的测量点 id。"""
    key = f"{stream_id}|{mtype}|{element or ''}"
    return "m_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Measurement:
    id: str
    stream_id: str
    mtype: str  # 'flow' | 'grade'
    value: float
    rsd: float
    sigma: float
    element: str | None = None


@dataclass
class Dataset:
    """一次考查某一版本的全部测量数据。"""

    measurements: dict[str, Measurement]  # id -> measurement

    def flow_on(self, stream_id: str) -> Measurement | None:
        return self.measurements.get(measurement_id(stream_id, "flow"))

    def grade_on(self, stream_id: str, element: str) -> Measurement | None:
        return self.measurements.get(measurement_id(stream_id, "grade", element))

    def streams_measured(self) -> set[str]:
        return {m.stream_id for m in self.measurements.values() if m.mtype == "flow"}


def build_dataset(
    raw: dict[str, Any],
    topology_elements: list[str],
    topology_stream_ids: list[str],
    rsd_overrides: dict[str, float] | None = None,
) -> Dataset:
    """解析并校验考查数据。

    :param raw: ``{"measurements": [{"stream_id", "type", "element",
        "value", "rsd"?}, ...]}``
    :param rsd_overrides: 按类别覆盖默认 RSD（``{"flow": ..., "grade": ...}``）
    """
    defaults = dict(DEFAULT_RSD)
    if rsd_overrides:
        for k, v in rsd_overrides.items():
            if k not in defaults:
                continue
            defaults[k] = float(v)

    items = raw.get("measurements")
    if not isinstance(items, list):
        from .errors import ValidationError

        raise ValidationError("dataset.measurements 必须是数组")

    stream_set = set(topology_stream_ids)
    element_set = set(topology_elements)
    measurements: dict[str, Measurement] = {}
    # 先算流量绝对下限（需要最大已测流量）
    parsed: list[tuple[str, str, str | None, float, float | None, str | None]] = []
    for item in items:
        if not isinstance(item, dict):
            from .errors import ValidationError

            raise ValidationError("每个测量必须是对象")
        sid = str(item.get("stream_id", ""))
        if sid not in stream_set:
            from .errors import ValidationError

            raise ValidationError(
                f"测量引用了拓扑中不存在的物流 {sid!r}", {"stream_id": sid}
            )
        mtype = str(item.get("type", ""))
        if mtype not in ("flow", "grade"):
            from .errors import ValidationError

            raise ValidationError(
                f"测量类型必须是 flow 或 grade，收到 {mtype!r}",
                {"stream_id": sid, "type": mtype},
            )
        element = item.get("element")
        if mtype == "grade":
            if element is None or str(element) not in element_set:
                from .errors import InconsistentElementsError

                raise InconsistentElementsError(
                    f"物流 {sid} 的品位测量缺少匹配的元素声明",
                    {"stream_id": sid, "element": element},
                )
            element = str(element)
        elif element is not None:
            from .errors import ValidationError

            raise ValidationError(
                f"流量测量不应带 element（物流 {sid}）", {"stream_id": sid}
            )
        value = item.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            from .errors import ValidationError

            raise ValidationError(
                "测量值必须是数值", {"stream_id": sid, "type": mtype}
            )
        value = float(value)
        if mtype == "flow" and value < 0:
            from .errors import NegativeFlowError

            raise NegativeFlowError(
                f"物流 {sid} 流量测量为负：{value}",
                {"stream_id": sid, "value": value},
            )
        if mtype == "grade" and not (0.0 <= value <= 100.0):
            from .errors import GradeRangeError

            raise GradeRangeError(
                f"物流 {sid} 元素 {element} 品位 {value} 不在 0~100% 之间",
                {"stream_id": sid, "element": element, "value": value},
            )
        rsd_raw = item.get("rsd")
        if rsd_raw is not None:
            if not isinstance(rsd_raw, (int, float)) or isinstance(rsd_raw, bool):
                from .errors import NonPositiveSigmaError

                raise NonPositiveSigmaError(
                    f"物流 {sid} 的 rsd 必须是正数", {"stream_id": sid}
                )
            if rsd_raw <= 0:
                from .errors import NonPositiveSigmaError

                raise NonPositiveSigmaError(
                    f"物流 {sid} 的相对标准差必须为正，收到 {rsd_raw}",
                    {"stream_id": sid, "rsd": rsd_raw},
                )
        mid = item.get("id")
        parsed.append((sid, mtype, element, value, (float(rsd_raw) if rsd_raw is not None else None), (str(mid) if mid is not None else None)))

    flow_values = [p[3] for p in parsed if p[1] == "flow"]
    flow_floor = FLOW_SIGMA_FLOOR_FRACTION * (max(flow_values) if flow_values else 1.0)

    for sid, mtype, element, value, rsd_in, mid_in in parsed:
        rsd = rsd_in if rsd_in is not None else defaults[mtype]
        if mtype == "flow":
            sigma = max(rsd * abs(value), flow_floor)
        else:
            sigma = max(rsd * abs(value), GRADE_SIGMA_FLOOR)
        mid = mid_in or measurement_id(sid, mtype, element)
        if mid in measurements:
            from .errors import DuplicateIdError

            raise DuplicateIdError(
                f"同一测量点出现多次：{mid}（{sid}/{mtype}/{element}）",
                {"measurement_id": mid},
            )
        measurements[mid] = Measurement(
            id=mid,
            stream_id=sid,
            mtype=mtype,
            value=value,
            rsd=rsd,
            sigma=sigma,
            element=element,
        )

    return Dataset(measurements=measurements)
