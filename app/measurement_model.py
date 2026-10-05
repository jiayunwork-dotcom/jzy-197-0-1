"""测量模型：考查数据、测量值与相对标准差默认值、变量与测量组装、权重。

接口层品位用百分数（0–100），内部统一用小数（0–1）；流量单位 t/h。
变量向量 x 的排列（全部按编码排序，保证编号/输入顺序无关）：
    [ 每股物流的干矿流量 F_s | 每个 (元素, 物流) 的品位 G_{e,s} ]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .errors import DomainError
from .topology import CircuitTopology

# ---- 默认相对标准差（经验起点，见 README 1.7；投用前应用本厂实测精密度替换）----
DEFAULT_FLOW_RSD_BELT = 0.015      # 皮带秤干矿流量
DEFAULT_FLOW_RSD_OTHER = 0.030     # 流量计折算 / 给矿估算 / 未注明
DEFAULT_GRADE_RSD_HIGH = 0.020     # 品位 >= 10%
DEFAULT_GRADE_RSD_MID = 0.040      # 1% <= 品位 < 10%
DEFAULT_GRADE_RSD_LOW = 0.080      # 品位 < 1%

GRADE_TIER_HIGH = 10.0
GRADE_TIER_LOW = 1.0


def default_grade_rsd(grade_percent: float) -> float:
    """按化验品位（百分数）分档取默认相对标准差。"""
    if grade_percent >= GRADE_TIER_HIGH:
        return DEFAULT_GRADE_RSD_HIGH
    if grade_percent >= GRADE_TIER_LOW:
        return DEFAULT_GRADE_RSD_MID
    return DEFAULT_GRADE_RSD_LOW


def default_flow_rsd(flow_kind: str | None) -> float:
    return DEFAULT_FLOW_RSD_BELT if flow_kind == "belt" else DEFAULT_FLOW_RSD_OTHER


@dataclass(frozen=True)
class FlowMeasurement:
    value: float                          # t/h
    rsd: Optional[float] = None           # 相对标准差；None 走默认
    flow_kind: Optional[str] = None       # belt | slurry | declared | None

    @classmethod
    def from_dict(cls, d: dict) -> "FlowMeasurement":
        return cls(value=float(d["value"]),
                   rsd=(float(d["rsd"]) if d.get("rsd") is not None else None),
                   flow_kind=d.get("flow_kind"))


@dataclass(frozen=True)
class GradeMeasurement:
    element: str
    value: float                          # 百分数 0..100
    rsd: Optional[float] = None

    @classmethod
    def from_dict(cls, d: dict) -> "GradeMeasurement":
        return cls(element=str(d["element"]), value=float(d["value"]),
                   rsd=(float(d["rsd"]) if d.get("rsd") is not None else None))


@dataclass(frozen=True)
class StreamData:
    code: str
    flow: Optional[FlowMeasurement] = None
    grades: tuple[GradeMeasurement, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "StreamData":
        return cls(
            code=str(d["code"]),
            flow=(FlowMeasurement.from_dict(d["flow"]) if d.get("flow") is not None
                  else None),
            grades=tuple(GradeMeasurement.from_dict(g) for g in d.get("grades", [])),
        )

    def to_dict(self) -> dict:
        return {"code": self.code,
                "flow": ({"value": self.flow.value, "rsd": self.flow.rsd,
                          "flow_kind": self.flow.flow_kind} if self.flow else None),
                "grades": [{"element": g.element, "value": g.value,
                            "rsd": g.rsd} for g in self.grades]}


@dataclass(frozen=True)
class SurveyData:
    """一次考查在某个数据版本下的全部测量数据。"""
    streams: tuple[StreamData, ...]
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SurveyData":
        return cls(streams=tuple(StreamData.from_dict(s)
                                 for s in d.get("streams", [])),
                   note=str(d.get("note", "")))

    def to_dict(self) -> dict:
        return {"streams": [s.to_dict() for s in self.streams], "note": self.note}

    def stream_map(self) -> dict[str, StreamData]:
        return {s.code: s for s in self.streams}


# ---- 测量编号：粗差报告/剔除集合里稳定标识一个测量 ----------------------------
def flow_mid(stream_code: str) -> str:
    return f"flow:{stream_code}"


def grade_mid(element: str, stream_code: str) -> str:
    return f"grade:{element}:{stream_code}"


def parse_mid(mid: str) -> tuple[str, str, Optional[str]]:
    """返回 (kind, stream_code, element|None)。"""
    parts = mid.split(":")
    if parts[0] == "flow" and len(parts) == 2:
        return "flow", parts[1], None
    if parts[0] == "grade" and len(parts) == 3:
        return "grade", parts[2], parts[1]
    raise DomainError(f"无法识别的测量编号: {mid}", "bad_measurement_id")


def validate_survey(topo: CircuitTopology, data: SurveyData) -> None:
    """数值合法性校验。结构上是否欠定（可观测性）在校正阶段判断。"""
    problems: list[dict] = []
    known = set(topo.stream_codes())
    seen_streams: set[str] = set()
    for sd in data.streams:
        if sd.code not in known:
            problems.append({"stream": sd.code, "reason": "物流不在所声明版本的拓扑中"})
            continue
        if sd.code in seen_streams:
            problems.append({"stream": sd.code, "reason": "同一物流重复出现"})
        seen_streams.add(sd.code)
        if sd.flow is not None:
            if not np.isfinite(sd.flow.value) or sd.flow.value < 0:
                problems.append({"measurement": flow_mid(sd.code),
                                 "reason": "流量必须为非负数"})
            if sd.flow.rsd is not None and (not np.isfinite(sd.flow.rsd)
                                           or sd.flow.rsd <= 0):
                problems.append({"measurement": flow_mid(sd.code),
                                 "reason": "相对标准差必须为正数"})
        seen_elem: set[str] = set()
        for g in sd.grades:
            mid = grade_mid(g.element, sd.code)
            if g.element not in topo.elements:
                problems.append({"measurement": mid,
                                 "reason": f"元素 {g.element} 不在拓扑声明的元素表中"})
            if g.element in seen_elem:
                problems.append({"measurement": mid, "reason": "同一元素品位重复出现"})
            seen_elem.add(g.element)
            if not np.isfinite(g.value) or not (0.0 <= g.value <= 100.0):
                problems.append({"measurement": mid,
                                 "reason": "品位必须在 0 到 100% 之间"})
            if g.rsd is not None and (not np.isfinite(g.rsd) or g.rsd <= 0):
                problems.append({"measurement": mid,
                                 "reason": "相对标准差必须为正数"})
    if problems:
        raise DomainError("考查数据校验未通过", "invalid_survey",
                          details={"problems": problems})


@dataclass
class VariableModel:
    """求解器所用的变量/测量布局与权重。"""
    stream_codes: list[str]
    elements: list[str]
    n_flow: int
    n_var: int
    flow_index: dict[str, int]
    grade_index: dict[tuple[str, str], int]
    measured: np.ndarray                 # bool[n_var]
    sigma: np.ndarray                    # 绝对标准差[n_var]，未测位为 +inf（权重 0）
    x_meas: np.ndarray                   # 测量值[n_var]，未测位 nan
    measurement_ids: list[Optional[str]] # 每个变量对应的测量编号（未测为 None）

    def is_grade(self, i: int) -> bool:
        return i >= self.n_flow

    def describe(self, i: int) -> dict:
        if i < self.n_flow:
            return {"kind": "flow", "stream": self.stream_codes[i]}
        k = i - self.n_flow
        e = self.elements[k // self.n_flow]
        s = self.stream_codes[k % self.n_flow]
        return {"kind": "grade", "element": e, "stream": s}


def build_variable_model(topo: CircuitTopology, data: SurveyData,
                         excluded: frozenset[str] = frozenset()) -> VariableModel:
    """组装变量布局与测量向量。

    excluded 中列出的测量编号视为未测（既不进目标函数，也不参与统计检验）。
    """
    stream_codes = sorted(topo.stream_codes())
    elements = sorted(topo.elements)
    nf = len(stream_codes)
    ne = len(elements)
    n_var = nf * (1 + ne)
    measured = np.zeros(n_var, dtype=bool)
    sigma = np.full(n_var, np.inf)
    x_meas = np.full(n_var, np.nan)
    mids: list[Optional[str]] = [None] * n_var
    flow_index = {c: i for i, c in enumerate(stream_codes)}
    grade_index = {(e, s): nf + ie * nf + is_
                   for ie, e in enumerate(elements)
                   for is_, s in enumerate(stream_codes)}

    smap = data.stream_map()
    for s_code in stream_codes:
        sd = smap.get(s_code)
        if sd is not None and sd.flow is not None:
            fid = flow_mid(s_code)
            if fid not in excluded:
                i = flow_index[s_code]
                rsd = sd.flow.rsd if sd.flow.rsd is not None \
                    else default_flow_rsd(sd.flow.flow_kind)
                measured[i] = True
                x_meas[i] = sd.flow.value
                sigma[i] = rsd * sd.flow.value if sd.flow.value > 0 else rsd
                mids[i] = fid
        if sd is not None:
            for g in sd.grades:
                gid = grade_mid(g.element, s_code)
                if gid in excluded:
                    continue
                j = grade_index[(g.element, s_code)]
                # 分档按接口百分数值；内部再换算成小数与分数绝对 σ
                rsd = g.rsd if g.rsd is not None else default_grade_rsd(g.value)
                g_frac = g.value / 100.0
                measured[j] = True
                x_meas[j] = g_frac
                sigma[j] = rsd * g_frac if g_frac > 0 else rsd / 100.0
                mids[j] = gid
    return VariableModel(
        stream_codes=stream_codes, elements=elements, n_flow=nf, n_var=n_var,
        flow_index=flow_index, grade_index=grade_index,
        measured=measured, sigma=sigma, x_meas=x_meas, measurement_ids=mids,
    )
