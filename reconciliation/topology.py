"""回路拓扑：节点 / 物流的数据结构、校验与关联矩阵。

约定
----
* 每股物流有 ``source``（起点）与 ``target``（终点）。节点平衡时，
  source -> node 的流量为“入”，node -> target 的流量为“出”。
* 端点取值为实际节点 id 或保留字 ``"@env"``，后者表示系统边界（环境），
  环境节点不参与平衡。例如给矿为 ``@env -> 给矿点``，精矿为 ``精选 -> @env``。
* 物流方向本身不限制正负物理意义，但节点守恒以关联矩阵符号为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import (
    DuplicateIdError,
    EndpointNotFoundError,
    IsolatedNodeError,
    TopologyError,
)

ENV_NODE = "@env"
"""系统边界保留 id。"""


@dataclass(frozen=True)
class Node:
    id: str
    name: str = ""
    kind: str = "generic"  # feeder/rougher/cleaner/scavener/junction/tailings/generic


@dataclass(frozen=True)
class Stream:
    id: str
    source: str
    target: str
    name: str = ""
    kind: str = "generic"  # feed/concentrate/tailings/middlings/intermediate/generic


@dataclass
class Topology:
    """校验过的回路拓扑。内部一律使用排好序的 id 列表，保证编号/顺序无关。"""

    nodes: list[Node]
    streams: list[Stream]
    elements: list[str]
    # 派生索引（按 id 排序，与输入顺序无关）
    node_ids: list[str] = field(default_factory=list)
    stream_ids: list[str] = field(default_factory=list)
    incidence: np.ndarray | None = None  # (n_internal_nodes, n_streams)，入为 +1 出为 -1

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def n_streams(self) -> int:
        return len(self.stream_ids)

    @property
    def n_elements(self) -> int:
        return len(self.elements)

    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def stream_map(self) -> dict[str, Stream]:
        return {s.id: s for s in self.streams}

    def boundary_streams(self) -> dict[str, list[str]]:
        """返回 {\"feed\": [...], \"product\": [...]} 的系统边界物流。

        feed：@env -> 内部节点；product：内部节点 -> @env。
        """
        smap = self.stream_map()
        feed = [sid for sid in self.stream_ids if smap[sid].source == ENV_NODE]
        product = [sid for sid in self.stream_ids if smap[sid].target == ENV_NODE]
        return {"feed": feed, "product": product}


def build_topology(raw: dict[str, Any]) -> Topology:
    """从原始 JSON 字典构建并校验拓扑。

    校验内容：
      * id 非空且不重复；
      * 物流端点必须存在（实际节点或 ``@env``）；
      * 不允许存在孤立节点（不与任何物流相连）；
      * 元素列表非空且不重复。
    """
    raw_nodes = raw.get("nodes")
    raw_streams = raw.get("streams")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise TopologyError("拓扑必须包含至少一个节点")
    if not isinstance(raw_streams, list):
        raise TopologyError("拓扑必须包含 streams 列表（可以为空）")

    nodes: list[Node] = []
    seen: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict) or not item.get("id"):
            raise TopologyError("节点必须包含非空 id")
        nid = str(item["id"])
        if nid == ENV_NODE:
            raise TopologyError(f"节点 id 不能使用保留字 {ENV_NODE!r}")
        if nid in seen:
            raise DuplicateIdError(f"节点 id 重复：{nid}", {"id": nid})
        seen.add(nid)
        nodes.append(
            Node(
                id=nid,
                name=str(item.get("name") or nid),
                kind=str(item.get("kind") or "generic"),
            )
        )

    streams: list[Stream] = []
    seen_s: set[str] = set()
    for item in raw_streams:
        if not isinstance(item, dict) or not item.get("id"):
            raise TopologyError("物流必须包含非空 id")
        sid = str(item["id"])
        if sid in seen_s:
            raise DuplicateIdError(f"物流 id 重复：{sid}", {"id": sid})
        seen_s.add(sid)
        src = str(item.get("source", ""))
        dst = str(item.get("target", ""))
        for endpoint, label in ((src, "source"), (dst, "target")):
            if endpoint != ENV_NODE and endpoint not in seen:
                raise EndpointNotFoundError(
                    f"物流 {sid} 的{label}端点 {endpoint!r} 不存在",
                    {"stream": sid, "endpoint": endpoint},
                )
        if src == dst and src != ENV_NODE:
            raise TopologyError(f"物流 {sid} 的起点与终点不能相同")
        streams.append(
            Stream(
                id=sid,
                source=src,
                target=dst,
                name=str(item.get("name") or sid),
                kind=str(item.get("kind") or "generic"),
            )
        )

    elements = _canonical_elements(raw.get("elements"))

    # 孤立节点：不与任何物流相连
    touched = {ENV_NODE}
    for s in streams:
        touched.add(s.source)
        touched.add(s.target)
    isolated = [n.id for n in nodes if n.id not in touched]
    if isolated:
        raise IsolatedNodeError(
            "存在不与任何物流相连的孤立节点", {"node_ids": isolated}
        )

    # 规范化：按 id 排序，确保后续计算与输入顺序、编号无关（仅内部排序，
    # 输出仍映射回原 id；编号无关性要求同一回路在不同 id 重命名下结果等价，
    # 由测试用同构拓扑对比保证）。
    nodes_sorted = sorted(nodes, key=lambda n: n.id)
    streams_sorted = sorted(streams, key=lambda s: s.id)
    node_ids = [n.id for n in nodes_sorted]
    stream_ids = [s.id for s in streams_sorted]

    n_index = {nid: i for i, nid in enumerate(node_ids)}
    A = np.zeros((len(node_ids), len(stream_ids)), dtype=float)
    for j, s in enumerate(streams_sorted):
        if s.source in n_index:  # node -> stream（出）：-1
            A[n_index[s.source], j] -= 1.0
        if s.target in n_index:  # stream -> node（入）：+1
            A[n_index[s.target], j] += 1.0

    return Topology(
        nodes=nodes_sorted,
        streams=streams_sorted,
        elements=elements,
        node_ids=node_ids,
        stream_ids=stream_ids,
        incidence=A,
    )


def _canonical_elements(raw_elements: Any) -> list[str]:
    if raw_elements is None:
        raise TopologyError("拓扑必须声明考查元素列表 elements")
    if isinstance(raw_elements, str):
        raw_elements = [raw_elements]
    if not isinstance(raw_elements, list) or not raw_elements:
        raise TopologyError("elements 必须是非空数组")
    out: list[str] = []
    seen: set[str] = set()
    for e in raw_elements:
        name = str(e).strip()
        if not name:
            raise TopologyError("元素名不能为空")
        if name in seen:
            raise DuplicateIdError(f"元素重复：{name}", {"element": name})
        seen.add(name)
        out.append(name)
    return sorted(out)
