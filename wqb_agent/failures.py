"""失败分类（Failure taxonomy）——从远程 Self-Evolution-wqb 参考实现吸收。

区分研究级失败（假设/方向/表达式本身错误，可学习）与系统级失败
（网络/认证/限流/超时/平台 5xx，环境问题，不可学习）。

只有研究级失败允许进入研究记忆（lessons / avoid / garbage）；
系统级失败绝不污染研究经验——它们不证明任何方向结论。

本地 Simulator 已有 UNKNOWN/FAILED 双态语义（UNKNOWN 表示 POST 可能已
发生但结果未知，进短期待对账）；本模块在其上提供统一的文本/状态码
分类，供 Reflection 决定一条 FAILED 实验是否值得写进 avoid。
"""

import re


# 失败类别
class FailureKind:
    RESEARCH = "RESEARCH"        # 假设/方向/表达式错误，可学习
    SYNTAX = "SYNTAX"            # 表达式语法/设置被平台拒绝（400/422）
    DATA = "DATA"                # 字段不存在 / 数据缺失（403/404/coverage）
    INFRA = "INFRA"              # 网络/连接/平台 5xx/服务不可用
    AUTH = "AUTH"                # 认证失败（401）
    RATE_LIMIT = "RATE_LIMIT"    # 限流（429）
    TIMEOUT = "TIMEOUT"          # 轮询/请求超时


# 由假设/表达式/字段选择引起、可以从中学到东西的类别；
# 其余是环境问题，必须留在研究记忆之外。
RESEARCH_RELEVANT = {
    FailureKind.RESEARCH,
    FailureKind.SYNTAX,
    FailureKind.DATA,
}


def is_research_relevant(kind):
    """该失败类别是否携带研究方向信息（可写入 avoid/lessons）。"""
    return kind in RESEARCH_RELEVANT


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
    最后默认 RESEARCH（研究级）。
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
    return FailureKind.RESEARCH


def classify_execution(execution):
    """Classify a transient execution record by status and error.

    - UNKNOWN：本地不确定语义，归为 TIMEOUT/INFRA 之外的特殊类，
      调用方应走短期待对账而非直接写入记忆。
    - FAILED：按 error 文本分类。
    """
    if execution.status == "UNKNOWN":
        return None  # 待对账，不属于任何可学习类别
    error = execution.error if isinstance(execution.error, str) else str(execution.error or "")
    # 新 client 的分类异常名（WQBError 子类），先按类名精确识别；
    # 再回退到文本特征（兼容旧 client 的字符串错误）。
    if "WQBAuthError" in error:
        return FailureKind.AUTH
    if "WQBRateLimitError" in error or "429" in error:
        return FailureKind.RATE_LIMIT
    if "WQBTimeoutError" in error or "timed out" in error.lower():
        return FailureKind.TIMEOUT
    if "WQBRejectedError" in error or "WQBNotFoundError" in error:
        return classify_error(error)
    if "Simulation rejected" in error or "422" in error or "400" in error:
        return FailureKind.SYNTAX
    if "404" in error or "not found" in error.lower():
        return FailureKind.DATA
    return FailureKind.INFRA
