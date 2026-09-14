"""Standalone, read-only Alpha template catalog and registry."""

from .loader import (
    PrivateTemplateCatalogError,
    load_builtin_templates,
    load_private_templates,
    load_templates,
    resolve_private_catalog_path,
)
from .model import AlphaTemplate, TemplateNumericSlot, TemplateOperatorSlot
from .registry import (
    DEFAULT_TEMPLATES,
    ECONOMIC_TEMPLATES,
    TEMPLATE_FIXED_NUMERICS,
    AlphaTemplateRegistry,
    template_numeric_audit,
)
from .validation import (
    RELATIONSHIP_CONTRACTS,
    SEMANTIC_CONTRACTS,
    effective_relationship_contract,
    effective_semantic_contract,
    evaluate_semantic_contract,
    validate_single_variable_change,
    validate_template_contract,
)

__all__ = [
    "AlphaTemplate",
    "TemplateNumericSlot",
    "TemplateOperatorSlot",
    "AlphaTemplateRegistry",
    "DEFAULT_TEMPLATES",
    "ECONOMIC_TEMPLATES",
    "TEMPLATE_FIXED_NUMERICS",
    "template_numeric_audit",
    "load_builtin_templates",
    "load_private_templates",
    "resolve_private_catalog_path",
    "PrivateTemplateCatalogError",
    "load_templates",
    "validate_template_contract",
    "validate_single_variable_change",
    "RELATIONSHIP_CONTRACTS",
    "SEMANTIC_CONTRACTS",
    "effective_relationship_contract",
    "effective_semantic_contract",
    "evaluate_semantic_contract",
]
