"""数据校正服务统一异常类型。

所有面向用户的校验失败都抛出 :class:`ValidationError` 的子类，
错误体为 ``code + message + details``，由接口层转成对应 HTTP 状态码。
"""

from __future__ import annotations

from typing import Any


class ReconciliationError(Exception):
    """本服务所有可预期异常的基类。"""

    code = "error"
    http_status = 400

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details if details is not None else {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(ReconciliationError):
    """请求数据不合法（结构、取值、拓扑关系）。"""

    code = "validation_error"
    http_status = 422


class TopologyError(ValidationError):
    code = "topology_invalid"


class IsolatedNodeError(TopologyError):
    code = "isolated_node"


class EndpointNotFoundError(TopologyError):
    code = "stream_endpoint_not_found"


class GradeRangeError(ValidationError):
    code = "grade_out_of_range"


class NegativeFlowError(ValidationError):
    code = "negative_flow"


class NonPositiveSigmaError(ValidationError):
    code = "non_positive_sigma"


class InconsistentElementsError(ValidationError):
    code = "inconsistent_elements"


class DuplicateIdError(ValidationError):
    code = "duplicate_id"


class UnobservableError(ValidationError):
    """存在无法由守恒关系唯一确定的未测变量。

    用户必须显式 ``accept_unobservable=true`` 才允许拿到非唯一解。
    """

    code = "unobservable"

    def __init__(self, message: str, unobservable: list[dict[str, Any]] | None = None):
        super().__init__(message, {"unobservable": unobservable or []})


class VersionConflictError(ReconciliationError):
    """乐观锁：提交修订时父版本已不是当前版本。"""

    code = "version_conflict"
    http_status = 409


class NotFoundError(ReconciliationError):
    code = "not_found"
    http_status = 404
