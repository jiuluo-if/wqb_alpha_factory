"""Canonical, dependency-free proposal schema vocabulary."""

PROPOSAL_EXPERIMENT_QS = {
    "field_understanding": "字段含义及其信息含义（必须基于本轮 discovery）",
    "operator_mapping": "算子如何表达该经济机制",
    "experiment_question": "本次 simulation 要回答的单一问题",
}

RESEARCH_ROLES = frozenset({"EXPLORE", "EXPLOIT", "VALIDATION"})
MAX_PROPOSALS_PER_ROUND = 18
MAX_CONFIGURED_PROPOSALS_PER_ROUND = 100
FACTORY_BATCH_SIZE = 100
TARGETED_BATCH_TYPE = "targeted_optimization"
MAX_TARGETED_CHILDREN = 4
MAX_TARGETED_VALIDATIONS = 4
MAX_TARGETED_PROPOSALS = MAX_TARGETED_CHILDREN + MAX_TARGETED_VALIDATIONS
TARGETED_BATCH_TTL_SEC = 6 * 3600
EXPERIMENT_STAGES = frozenset({"BASELINE", "CHILD", "ROBUSTNESS"})
CHILD_CHANGE_TYPES = frozenset({
    "field_swap", "window_change", "operator_variant", "smoothing",
    "neutralization", "decay", "window_locality", "semantic_field_swap",
    "universe", "universe_robustness", "decay_truncation",
})
SETTING_OVERRIDES = frozenset({"universe", "truncation", "decay"})
