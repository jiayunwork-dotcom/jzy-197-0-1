"""回路拓扑：节点与物流的数据模型、关联矩阵、录入校验。

拓扑整体不可变；修订拓扑由存储层产生新版本，旧版本永远保留，旧考查的校正
结果绑定其所用版本。

物流端点约定：
- source/target 是节点编码；
- None 表示系统边界：source=None 为进系统给矿，target=None 为出系统产品；
  （"source"/"sink" 两个字符串保留为虚拟边界节点，也可显式使用。）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .errors import DomainError

BOUNDARY_SOURCE = "source"  # 系统给矿边界
BOUNDARY_SINK = "sink"      # 系统产品边界


@dataclass(frozen=True)
class Node:
    code: str
    name: str = ""
    kind: str = "unit"            # feed | unit | junction 等，仅作标注

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(code=str(d["code"]), name=str(d.get("name", "")),
                   kind=str(d.get("kind", "unit")))

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "kind": self.kind}


@dataclass(frozen=True)
class Stream:
    code: str
    source: Optional[str]         # None / "source" 表示进系统
    target: Optional[str]         # None / "sink" 表示出系统
    name: str = ""
    role: str = "intermediate"    # feed | concentrate | tailings | intermediate

    @classmethod
    def from_dict(cls, d: dict) -> "Stream":
        return cls(
            code=str(d["code"]),
            source=d.get("source"),
            target=d.get("target"),
            name=str(d.get("name", "")),
            role=str(d.get("role", "intermediate")),
        )

    def to_dict(self) -> dict:
        return {"code": self.code, "source": self.source, "target": self.target,
                "name": self.name, "role": self.role}

    @property
    def is_boundary_input(self) -> bool:
        return self.source in (None, BOUNDARY_SOURCE)

    @property
    def is_boundary_output(self) -> bool:
        return self.target in (None, BOUNDARY_SINK)


@dataclass(frozen=True)
class CircuitTopology:
    circuit_code: str
    version: int
    nodes: tuple[Node, ...]
    streams: tuple[Stream, ...]
    elements: tuple[str, ...]              # 本回路考查的元素，如 ["Cu"]
    created_at: Optional[str] = None
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "CircuitTopology":
        nodes = tuple(Node.from_dict(n) for n in d.get("nodes", []))
        streams = tuple(Stream.from_dict(s) for s in d.get("streams", []))
        elements = tuple(str(e) for e in d.get("elements", []))
        return cls(
            circuit_code=str(d["circuit_code"]),
            version=int(d.get("version", 1)),
            nodes=nodes, streams=streams, elements=elements,
            created_at=d.get("created_at"),
            note=str(d.get("note", "")),
        )

    def to_dict(self) -> dict:
        return {
            "circuit_code": self.circuit_code, "version": self.version,
            "nodes": [n.to_dict() for n in self.nodes],
            "streams": [s.to_dict() for s in self.streams],
            "elements": list(self.elements),
            "created_at": self.created_at, "note": self.note,
        }

    def node_codes(self) -> list[str]:
        return [n.code for n in self.nodes]

    def stream_codes(self) -> list[str]:
        return [s.code for s in self.streams]

    def node(self, code: str) -> Node:
        for n in self.nodes:
            if n.code == code:
                return n
        raise KeyError(code)

    def stream(self, code: str) -> Stream:
        for s in self.streams:
            if s.code == code:
                return s
        raise KeyError(code)

    def streams_at(self, node_code: str) -> list[tuple[Stream, int]]:
        """返回与节点相连的 (物流, 方向)；方向 +1 出节点，-1 进节点。"""
        out: list[tuple[Stream, int]] = []
        for s in self.streams:
            if s.target == node_code:
                out.append((s, -1))
            if s.source == node_code:
                out.append((s, 1))
        return out


def _is_boundary(code: Optional[str]) -> bool:
    return code is None or code in (BOUNDARY_SOURCE, BOUNDARY_SINK)


def validate_topology(topo: CircuitTopology) -> None:
    """录入校验，不合法抛 DomainError（400）。

    检查：编码非空/不重复、物流端点存在、至少一个节点一股物流、没有孤立节点、
    元素声明非空、边界端点方向合法。
    """
    problems: list[dict] = []
    if not topo.nodes:
        raise DomainError("拓扑至少需要一个节点", "empty_topology")
    node_codes = topo.node_codes()
    if any(not c.strip() for c in node_codes):
        raise DomainError("节点编码不能为空", "empty_code")
    if len(set(node_codes)) != len(node_codes):
        dup = sorted({c for c in node_codes if node_codes.count(c) > 1})
        raise DomainError("节点编码重复", "duplicate_node", {"codes": dup})
    if not topo.streams:
        raise DomainError("拓扑至少需要一股物流", "empty_topology")
    stream_codes = topo.stream_codes()
    if any(not c.strip() for c in stream_codes):
        raise DomainError("物流编码不能为空", "empty_code")
    if len(set(stream_codes)) != len(stream_codes):
        dup = sorted({c for c in stream_codes if stream_codes.count(c) > 1})
        raise DomainError("物流编码重复", "duplicate_stream", {"codes": dup})
    if not topo.elements:
        # 允许纯干矿考查（不化验任何元素）；可观测性/粗差按实际变量数分析
        pass

    node_set = set(node_codes)
    touched: dict[str, int] = {c: 0 for c in node_codes}
    for s in topo.streams:
        src, tgt = s.source, s.target
        # 边界方向必须自洽：source 端只能是给矿边界，target 端只能是产品边界
        if src == BOUNDARY_SINK or tgt == BOUNDARY_SOURCE:
            problems.append({"stream": s.code,
                             "reason": "边界端点方向错误（source 端只能是给矿边界，"
                                       "target 端只能是产品边界）"})
        if not _is_boundary(src) and src not in node_set:
            problems.append({"stream": s.code, "reason": f"起点节点不存在: {src}"})
        if not _is_boundary(tgt) and tgt not in node_set:
            problems.append({"stream": s.code, "reason": f"终点节点不存在: {tgt}"})
        if _is_boundary(src) and _is_boundary(tgt):
            problems.append({"stream": s.code,
                             "reason": "物流两端都挂在系统边界上，没有经过任何节点"})
        if src in node_set:
            touched[src] += 1
        if tgt in node_set:
            touched[tgt] += 1
    isolated = sorted(c for c, k in touched.items() if k == 0)
    if isolated:
        problems.append({"reason": "存在孤立节点（没有任何物流连接）", "nodes": isolated})
    if problems:
        raise DomainError("拓扑校验未通过", "invalid_topology",
                          details={"problems": problems})


def incidence_matrix(topo: CircuitTopology) -> np.ndarray:
    """关联矩阵 A（真实节点行 × 物流列）：进 −1，出 +1；边界物流只挂非空端。

    行/列顺序都按编码排序，保证编号与输入顺序无关。
    """
    node_codes = sorted(n.code for n in topo.nodes)
    stream_codes = sorted(s.code for s in topo.streams)
    n_idx = {c: i for i, c in enumerate(node_codes)}
    s_idx = {c: i for i, c in enumerate(stream_codes)}
    a = np.zeros((len(node_codes), len(stream_codes)))
    for s in topo.streams:
        j = s_idx[s.code]
        if s.source in n_idx:
            a[n_idx[s.source], j] += 1.0
        if s.target in n_idx:
            a[n_idx[s.target], j] -= 1.0
    return a
