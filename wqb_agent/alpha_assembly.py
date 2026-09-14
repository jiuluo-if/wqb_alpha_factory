"""Pure candidate metadata assembly helpers."""

from __future__ import annotations

from collections.abc import Mapping


def field_mechanism(
    profile: Mapping[str, object],
    traits: Mapping[str, object],
    template: Mapping[str, object],
    relation: Mapping[str, object] | None = None,
) -> str:
    field_id = str(profile.get("id"))
    admission = traits.get("semantic_admission", "UNKNOWN")
    family = template.get("family", "unknown")
    if admission != "ALLOW":
        return (
            f"字段 {field_id} 的语义准入为 {admission}；"
            f"当前 profile 只能支持 {family} 的语法审阅，不能证明该字段具备该经济机制。"
        )
    fit_reason = {
        "analyst_revision": "修正值直接承载分析师预期更新，适合检验变化、持续性或滞后确认",
        "option_relative": "put-call/skew 字段表达期权分布的相对位置，适合离散或相对关系检验",
        "liquidity": "交易活跃度或未平仓量描述参与程度，适合流动性与活动强度检验",
        "volatility": "波动率是风险暴露或状态变量，适合风险调整、regime 或相对关系",
        "fundamental": "低频基本面水平代表经济规模，适合持久性和相对状态检验",
        "earnings": "盈利相关字段承载经营预期，适合变化与信息扩散检验",
        "event_count": "事件计数代表注意力事件强度，适合事件发生后的变化检验",
        "data_quality": "数据质量字段描述可用性风险，只进入缺失或陈旧信息机制",
    }.get(
        traits.get("concept"),
        f"该字段的 {traits.get('measurement')} 测量与 {family} 的有限结构相容",
    )
    mechanism = (
        f"字段 {field_id} 被识别为 {traits.get('concept')}，测量为 {traits.get('measurement')}，"
        f"频率为 {traits.get('frequency')}，符号语义为 {traits.get('sign_semantics')}，"
        f"行为为 {traits.get('behavior')}；{fit_reason}。"
        "该机制仍需用独立样本和平台 checks 证伪。"
    )
    if relation and relation.get("labels"):
        mechanism += f" 槽位关系证据为：{', '.join(relation['labels'])}。"
    return mechanism
