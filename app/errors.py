"""领域错误与请求校验错误。

所有用户可纠正的输入/状态问题都抛 DomainError，由 Flask 错误处理器统一转成
JSON 响应；错误码 code 稳定，供前端和测试区分。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误。http_status 给出建议 HTTP 状态码。"""

    def __init__(self, message: str, code: str = "invalid_request",
                 http_status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.details = details or {}

    def to_dict(self) -> dict:
        d = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            d["error"]["details"] = self.details
        return d


class NotFoundError(DomainError):
    def __init__(self, message: str, code: str = "not_found"):
        super().__init__(message, code=code, http_status=404)


class ConflictError(DomainError):
    def __init__(self, message: str, code: str = "conflict", details: dict | None = None):
        super().__init__(message, code=code, http_status=409, details=details)


class UnobservableError(DomainError):
    """存在不能唯一确定的未测变量，且调用方未明确接受该结果。"""

    def __init__(self, message: str, details: dict):
        super().__init__(message, code="unobservable", http_status=422,
                         details=details)


class NotConvergedError(DomainError):
    """预留：达到迭代上限（默认不作为错误抛出，结果里带 not_converged 状态）。"""

    def __init__(self, message: str, details: dict):
        super().__init__(message, code="not_converged", http_status=200,
                         details=details)
