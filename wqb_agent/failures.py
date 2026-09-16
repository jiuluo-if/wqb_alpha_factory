"""Classify remote transport failures for the WQB client.

This module describes request/response handling only. It does not infer
research outcomes or persist failure facts for an optimizer.
"""

import re


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
