"""共享夹具：一进两出最小回路与几组数据。"""

from __future__ import annotations

import pytest

from api.app import create_app
from reconciliation.repository import InMemoryRepository

# 手算核对基准（给矿 100 t/h @2.0%，精矿 20%，尾矿 0.5%，两股产品无流量）：
# F_c = 100*(2.0-0.5)/(20-0.5) = 7.692307..., F_t = 92.307692...,
# 精矿回收率 = 76.923076...%
EXPECTED_CONC_FLOW = 100.0 * (2.0 - 0.5) / (20.0 - 0.5)
EXPECTED_TAIL_FLOW = 100.0 - EXPECTED_CONC_FLOW
EXPECTED_RECOVERY = 100.0 * EXPECTED_CONC_FLOW * 20.0 / (100.0 * 2.0)


def one_in_two_out_topology(node_id="splitter", feed="feed", conc="conc", tail="tail"):
    return {
        "nodes": [{"id": node_id, "name": "给矿/粗选合并点", "kind": "feeder"}],
        "streams": [
            {"id": feed, "source": "@env", "target": node_id, "kind": "feed"},
            {"id": conc, "source": node_id, "target": "@env", "kind": "concentrate"},
            {"id": tail, "source": node_id, "target": "@env", "kind": "tailings"},
        ],
        "elements": ["Cu"],
    }


def handcalc_dataset(feed="feed", conc="conc", tail="tail", feed_flow=100.0):
    return {
        "measurements": [
            {"stream_id": feed, "type": "flow", "value": feed_flow},
            {"stream_id": feed, "type": "grade", "element": "Cu", "value": 2.0},
            {"stream_id": conc, "type": "grade", "element": "Cu", "value": 20.0},
            {"stream_id": tail, "type": "grade", "element": "Cu", "value": 0.5},
        ]
    }


def fully_measured_balanced_dataset():
    """所有流量、品位都测，且数值与平衡完全一致（二进制精确表示）。

    取 F_c = 5, F_t = 95；满足 100*g_f = 5*g_c + 95*g_t：
    令 g_f = 2.0, g_t = 1.0 -> 200 = 5 g_c + 95 -> g_c = 21.0（精确）。
    """
    return {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "conc", "type": "flow", "value": 5.0},
            {"stream_id": "tail", "type": "flow", "value": 95.0},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 2.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 21.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 1.0},
        ]
    }


@pytest.fixture
def topo():
    return one_in_two_out_topology()


@pytest.fixture
def dataset():
    return handcalc_dataset()


@pytest.fixture
def app():
    app = create_app(InMemoryRepository())
    app.testing = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()
