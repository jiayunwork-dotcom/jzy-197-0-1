"""输入拒收规则测试：拓扑与测量取值校验。"""

from __future__ import annotations

import pytest

from reconciliation.errors import (
    EndpointNotFoundError,
    GradeRangeError,
    IsolatedNodeError,
    NegativeFlowError,
    NonPositiveSigmaError,
)
from reconciliation.measurements import build_dataset
from reconciliation.topology import build_topology


def _raw(extra_stream=None, nodes=None):
    return {
        "nodes": nodes or [{"id": "n"}],
        "streams": [
            {"id": "f", "source": "@env", "target": "n"},
            {"id": "c", "source": "n", "target": "@env"},
        ],
        "elements": ["Cu"],
    }


def test_isolated_node_rejected():
    raw = {
        "nodes": [{"id": "n1"}, {"id": "orphan"}],
        "streams": [{"id": "f", "source": "@env", "target": "n1"}],
        "elements": ["Cu"],
    }
    with pytest.raises(IsolatedNodeError):
        build_topology(raw)


def test_missing_endpoint_rejected():
    raw = {
        "nodes": [{"id": "n1"}],
        "streams": [{"id": "f", "source": "@env", "target": "ghost"}],
        "elements": ["Cu"],
    }
    with pytest.raises(EndpointNotFoundError):
        build_topology(raw)


@pytest.fixture
def topology(topo):
    return build_topology(topo)


def test_grade_out_of_range_rejected(topology):
    for bad in (-0.1, 100.1, 120):
        data = {"measurements": [
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": bad}
        ]}
        with pytest.raises(GradeRangeError):
            build_dataset(data, topology.elements, topology.stream_ids)


def test_negative_flow_rejected(topology):
    data = {"measurements": [
        {"stream_id": "feed", "type": "flow", "value": -3.0}
    ]}
    with pytest.raises(NegativeFlowError):
        build_dataset(data, topology.elements, topology.stream_ids)


def test_non_positive_sigma_rejected(topology):
    for bad in (0.0, -0.02):
        data = {"measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0, "rsd": bad}
        ]}
        with pytest.raises(NonPositiveSigmaError):
            build_dataset(data, topology.elements, topology.stream_ids)


def test_measurement_on_unknown_stream_rejected(topology):
    from reconciliation.errors import ValidationError

    data = {"measurements": [
        {"stream_id": "nope", "type": "flow", "value": 1.0}
    ]}
    with pytest.raises(ValidationError):
        build_dataset(data, topology.elements, topology.stream_ids)
