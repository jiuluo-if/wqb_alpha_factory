"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Validate agent-authored proposals before execution.
READ WHEN: changing schema, field/operator provenance, or budget gates.
DO NOT USE FOR: selecting hypotheses or bypassing fail-closed validation.

Pure proposal contract and allocation validation.

This module has no transport, persistence, or Agent dependency.  Keeping the
proposal contract here lets the long-running orchestrator compose discovery,
execution, and state updates without owning every validation rule itself.
"""

from . import proposal_batch as _proposal_batch
from . import proposal_schema as _proposal_schema
from . import proposal_validation as _proposal_validation

CHILD_CHANGE_TYPES = _proposal_schema.CHILD_CHANGE_TYPES
EXPERIMENT_STAGES = _proposal_schema.EXPERIMENT_STAGES
FACTORY_BATCH_SIZE = _proposal_schema.FACTORY_BATCH_SIZE
MAX_CONFIGURED_PROPOSALS_PER_ROUND = _proposal_schema.MAX_CONFIGURED_PROPOSALS_PER_ROUND
MAX_PROPOSALS_PER_ROUND = _proposal_schema.MAX_PROPOSALS_PER_ROUND
MAX_TARGETED_CHILDREN = _proposal_schema.MAX_TARGETED_CHILDREN
MAX_TARGETED_PROPOSALS = _proposal_schema.MAX_TARGETED_PROPOSALS
MAX_TARGETED_VALIDATIONS = _proposal_schema.MAX_TARGETED_VALIDATIONS
PROPOSAL_EXPERIMENT_QS = _proposal_schema.PROPOSAL_EXPERIMENT_QS
RESEARCH_ROLES = _proposal_schema.RESEARCH_ROLES
SETTING_OVERRIDES = _proposal_schema.SETTING_OVERRIDES
TARGETED_BATCH_TTL_SEC = _proposal_schema.TARGETED_BATCH_TTL_SEC
TARGETED_BATCH_TYPE = _proposal_schema.TARGETED_BATCH_TYPE

# Compatibility facade: implementation lives in canonical modules.
factory_batch_stats = _proposal_batch.factory_batch_stats
proposal_budget_cap = _proposal_batch.proposal_budget_cap
targeted_batch_state = _proposal_batch.targeted_batch_state
validate_factory_batch = _proposal_batch.validate_factory_batch
validate_targeted_batch = _proposal_batch.validate_targeted_batch
_expression_operators = _proposal_validation._expression_operators
_operator_reference = _proposal_validation._operator_reference
load_operator_syntax_reference = _proposal_validation.load_operator_syntax_reference
load_packaged_operator_syntax_reference = _proposal_validation.load_packaged_operator_syntax_reference
proposal_priority = _proposal_validation.proposal_priority
validate_proposal = _proposal_validation.validate_proposal
validate_self_correlation_impact = _proposal_validation.validate_self_correlation_impact
validate_vector_inputs = _proposal_validation.validate_vector_inputs
