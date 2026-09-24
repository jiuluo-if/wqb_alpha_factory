"""Classify remote transport failures for the WQB client.

This module describes request/response handling only. It does not infer
research outcomes or persist failure facts for an optimizer.
"""

import re

REASON_CODES = frozenset({
    "INVALID_SPEC", "CAPABILITY_UNAVAILABLE", "EXACT_DUPLICATE",
    "RATE_LIMIT_OR_SUBMIT_UNKNOWN", "AUTH_FAILURE", "POLL_PENDING",
    "NOT_DISPATCHED", "NEW_PROBE_REQUIRED",
})


class ResearchReasonError(ValueError):
    """Human-readable validation error with one stable Agent reason code."""

    def __init__(self, message, reason_code):
        if reason_code not in REASON_CODES:
            raise ValueError(f"unsupported reason_code: {reason_code}")
        super().__init__(str(message))
        self.reason_code = reason_code


# 失败类别
class FailureKind:
    SYNTAX = "SYNTAX"            # 表达式语法/设置被平台拒绝（400/422）
    DATA = "DATA"                # 字段不存在 / 数据缺失（403/404/coverage）
    INFRA = "INFRA"              # 网络/连接/平台 5xx/服务不可用
    AUTH = "AUTH"                # 认证失败（401）
    RATE_LIMIT = "RATE_LIMIT"    # 限流（429）
    TIMEOUT = "TIMEOUT"          # 轮询/请求超时


_TIMEOUT_RE = re.compile(r"timeout|timed out", re.IGNORECASE)
_AUTH_RE = re.compile(r"auth|credential|401|login|forbidden|403", re.IGNORECASE)
_RATE_RE = re.compile(r"429|rate.?limit|too many requests|retry-after",
                      re.IGNORECASE)
_SYNTAX_RE = re.compile(r"rejected|422|400|syntax|parse|invalid expression",
                        re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"404|not found|missing|unknown field|no such",
                           re.IGNORECASE)
_INFRA_RE = re.compile(r"5\d\d|connection|broken|unavailable|refused|"
                       r"network|temporary|proxy", re.IGNORECASE)


def classify_error(error_text, status_code=None):
    """把一条错误（消息文本 + 可选状态码）映射为 FailureKind。

    状态码优先（确定性），文本正则兜底。顺序与远程参考实现一致：
    AUTH(401) > RATE_LIMIT(429) > SYNTAX(400/422) > DATA(403/404) > INFRA(5xx)，
    然后按文本特征依次匹配 timeout/auth/rate/syntax/not-found/infra，
    最后默认 INFRA。
    """
    text = error_text if isinstance(error_text, str) else str(error_text or "")
    if status_code == 401:
        return FailureKind.AUTH
    if status_code == 429:
        return FailureKind.RATE_LIMIT
    if status_code in (400, 422):
        return FailureKind.SYNTAX
    if status_code in (403, 404):
        return FailureKind.DATA
    if status_code is not None and status_code >= 500:
        return FailureKind.INFRA

    if _TIMEOUT_RE.search(text):
        return FailureKind.TIMEOUT
    if _AUTH_RE.search(text):
        return FailureKind.AUTH
    if _RATE_RE.search(text):
        return FailureKind.RATE_LIMIT
    if _SYNTAX_RE.search(text):
        return FailureKind.SYNTAX
    if _NOT_FOUND_RE.search(text):
        return FailureKind.DATA
    if _INFRA_RE.search(text):
        return FailureKind.INFRA
    return FailureKind.INFRA


def reason_code_for_failure(status, error=None, *, progress_url=None):
    """Project common execution failures into a small stable Agent vocabulary."""
    status = str(status or "").strip().upper()
    explicit = getattr(error, "reason_code", None)
    if explicit in REASON_CODES:
        return explicit
    if status == "EXACT_DUPLICATE":
        return "EXACT_DUPLICATE"
    if status == "NOT_DISPATCHED":
        return "NOT_DISPATCHED"
    text = str(error or "")
    if "NEW_PROBE_REQUIRED" in text:
        return "NEW_PROBE_REQUIRED"
    if "CAPABILITY" in text or "PERMISSION_UNAVAILABLE" in text:
        return "CAPABILITY_UNAVAILABLE"
    if progress_url and status in {"PENDING", "RUNNING", "UNKNOWN"}:
        return "POLL_PENDING"
    if status == "SUBMIT_UNKNOWN":
        return "RATE_LIMIT_OR_SUBMIT_UNKNOWN"
    kind = str(getattr(error, "kind", "") or "").upper()
    if kind == FailureKind.AUTH or classify_error(text) == FailureKind.AUTH:
        return "AUTH_FAILURE"
    if kind == FailureKind.RATE_LIMIT or (
        status in {"UNKNOWN", "FAILED"}
        and classify_error(text) in {FailureKind.RATE_LIMIT, FailureKind.TIMEOUT, FailureKind.INFRA}
    ):
        return "RATE_LIMIT_OR_SUBMIT_UNKNOWN"
    if status in {"INVALID", "INVALID_SPEC", "FAILED"} and (
        kind in {FailureKind.SYNTAX, FailureKind.DATA}
        or classify_error(text) in {FailureKind.SYNTAX, FailureKind.DATA}
    ):
        return "INVALID_SPEC"
    return None
