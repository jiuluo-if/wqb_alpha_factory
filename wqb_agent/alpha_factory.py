"""Template-first Alpha candidate factory.

This is a proposal-construction layer only.  It does not call BRAIN, write
research state, or submit simulations.  A template describes the structural
shape of an Alpha; a field bundle supplies the slots.  The resulting metadata
keeps the skeleton visible to the later proposal and diversity gates.
"""

import itertools
import random

from .alpha_templates import (
    DEFAULT_TEMPLATES,
    ECONOMIC_TEMPLATES,
    AlphaTemplate,
    AlphaTemplateRegistry,
    TemplateNumericSlot,
    template_numeric_audit,
)
from .discovery import frequency_evidence, normalize_coverage
from .diversity import (
    extract_fields,
    select_budget_candidates,
    semantic_mechanism_key_from_traits,
)
from .expression import analyze_expression, canonical_expression
from .pre_correlation import optimization_parent_admission
from .proposal_contract import CHILD_CHANGE_TYPES, FACTORY_BATCH_SIZE
from .research_guard import overfit_expression_reason, parameter_only_change_reason

__all__ = [
    "AlphaFactory", "AlphaTemplate", "AlphaTemplateRegistry",
    "TemplateNumericSlot", "DEFAULT_TEMPLATES", "ECONOMIC_TEMPLATES",
    "template_numeric_audit",
]

MAX_TEMPLATE_FAMILY_PER_BATCH = 2

# 参数验证只复用既有 change_type 词表：window / decay / truncation / universe
# 的变化不是新经济机制，只能进入 ROBUSTNESS。
VALIDATION_CHANGE_TYPES = {
    "decay": "decay",
    "truncation": "decay_truncation",
    "universe": "universe",
    "template_window": "window_change",
}


_SEMANTIC_CONCEPT_RULES = (
    ("data_quality", ("missing", "null", "nan", "quality", "coverage", "stale")),
    ("analyst_revision", ("revision", "revised", "estimate change", "forecast change")),
    ("option_relative", ("put call", "put-call", "putcall", "iv skew")),
    ("liquidity", ("open interest", "option volume", "liquidity", "trading volume", "dollar volume", "turnover", "bid ask", "bid-ask")),
    ("volatility", ("volatility", "implied vol", "realized vol", "iv_skew", "variance")),
    ("event_count", ("mention count", "event count", "number of events", "occurrence", "filing count")),
    ("sentiment", ("sentiment", "social", "news", "recommendation", "bullish", "bearish")),
    ("valuation", ("valuation", "target price", "price target", "price-to", "price to", "multiple", "p/e", "p/b")),
    ("earnings", ("earnings", "eps", "revenue", "sales", "profit", "cash flow", "fscore")),
    ("fundamental", ("total assets", "assets", "liabilities", "equity", "book value", "debt", "fundamental")),
    ("market_price", ("price", "close", "open", "high", "low", "vwap", "return")),
)

_SEMANTIC_MEASUREMENT_RULES = (
    ("dispersion", ("dispersion", "skew", "spread", "standard deviation", "std dev")),
    ("ratio", ("ratio", "percent", "%", "margin", "yield", "multiple", "p/e", "p/b")),
    ("change", ("revision", "revised", "change", "delta", "growth", "return", "momentum", "surprise", "diff")),
    ("count", ("count", "number", "mentions", "events", "occurrence", "volume")),
    ("probability", ("probability", "likelihood", "rating", "recommendation")),
)

_SEMANTIC_RELATION_LABELS = {
    "same_economic_concept",
    "numerator_denominator",
    "complementary_expectations",
    "comparable_scale",
    "price_volume",
    "option_pair",
    "revision_dispersion",
}


def _semantic_profile_text(profile, *, include_dataset=True):
    """Build semantic evidence only from the documented field profile keys."""
    if not isinstance(profile, dict):
        return ""
    values = []
    keys = ("id", "name", "description")
    if include_dataset:
        keys = (*keys, "dataset", "frequency", "category")
    for key in keys:
        value = profile.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("name")
        if value is not None:
            values.append(str(value))
    return " ".join(values).lower().replace("_", " ")


def _semantic_has(text, phrase):
    phrase = str(phrase).lower()
    if not phrase:
        return False
    if any(char.isalnum() for char in phrase):
        return phrase in text
    return phrase in text


def _semantic_frequency_text(profile):
    value = profile.get("frequency") if isinstance(profile, dict) else None
    if isinstance(value, dict):
        value = value.get("name") or value.get("id")
    return str(value or "").lower()


def _derive_field_semantic_traits(profile):
    """Derive a conservative, non-persistent semantic view of one profile."""
    if not isinstance(profile, dict):
        return {
            "concept": "unknown",
            "measurement": "unknown",
            "behavior": "unknown",
            "frequency": "unknown",
            "sign_semantics": "unknown",
            "direction_meaning": "unknown",
            "update_style": "unknown",
            "tags": [],
            "status": "UNKNOWN",
            "semantic_admission": "UNKNOWN",
            "metadata_semantics": "UNKNOWN",
        }
    text = _semantic_profile_text(profile)
    # Category and dataset are fallback context, not direct field evidence.
    # Keep them out of the high-confidence admission path.
    direct_text = _semantic_profile_text(profile, include_dataset=False)
    category = str(profile.get("category") or "").lower()
    frequency = _semantic_frequency_text(profile)

    concept = "unknown"
    concept_hits = []
    direct_concept_hits = []
    for candidate, keywords in _SEMANTIC_CONCEPT_RULES:
        hits = [word for word in keywords if _semantic_has(text, word)]
        if hits:
            concept = candidate
            concept_hits = hits
            direct_concept_hits = [
                word for word in keywords if _semantic_has(direct_text, word)
            ]
            break
    if concept == "unknown":
        category_rules = {
            "analyst": "analyst",
            "option": "option",
            "options": "option",
            "fundamental": "fundamental",
            "social": "sentiment",
            "news": "sentiment",
            "liquidity": "liquidity",
        }
        concept = category_rules.get(category, "unknown")
        concept_hits = [category] if concept != "unknown" else []
        direct_concept_hits = []

    measurement = "level"
    measurement_hits = []
    for candidate, keywords in _SEMANTIC_MEASUREMENT_RULES:
        hits = [word for word in keywords if _semantic_has(text, word)]
        if hits:
            measurement = candidate
            measurement_hits = hits
            break
    if concept == "analyst_revision":
        measurement = "change"
        if "revision" not in measurement_hits:
            measurement_hits = ["revision"] + measurement_hits
    if concept == "event_count":
        measurement = "count"
    if (
        measurement == "dispersion"
        and "analyst" in direct_text
        and concept != "analyst_revision"
    ):
        concept = "analyst_dispersion"
        concept_hits = ["analyst", "dispersion"]
        direct_concept_hits = ["analyst", "dispersion"]

    frequency_slow = any(
        marker in frequency for marker in ("quarter", "monthly", "month", "annual", "year", "weekly", "week")
    )
    frequency_fast = any(
        marker in frequency for marker in ("intraday", "minute", "hour", "daily", "day")
    )
    event_signal = concept in {
        "analyst_revision", "analyst_dispersion", "event_count", "sentiment"
    }
    slow_signal = frequency_slow or concept in {"fundamental", "earnings", "valuation"} and not frequency_fast
    sparse = False
    coverage = normalize_coverage(profile)
    sparse = coverage is not None and coverage < 0.5
    if slow_signal:
        behavior = "slow_moving"
    elif event_signal:
        behavior = "event_driven"
    elif sparse:
        behavior = "sparse"
    elif measurement in {"change", "dispersion"} or concept in {"market_price", "volatility", "liquidity"}:
        behavior = "signed"
    elif measurement in {"ratio", "probability"}:
        behavior = "bounded"
    elif measurement == "count" or concept in {"event_count", "fundamental"}:
        behavior = "nonnegative"
    else:
        behavior = "unknown"

    if event_signal:
        update_style = "event_driven"
    elif slow_signal:
        update_style = "periodic"
    elif frequency_fast:
        update_style = "continuous"
    else:
        update_style = "unknown"

    if concept == "analyst_revision" or measurement == "change":
        sign_semantics = "signed_change"
    elif concept in {"volatility", "event_count", "fundamental", "earnings", "data_quality"}:
        sign_semantics = "nonnegative_level"
    elif measurement == "dispersion":
        sign_semantics = "nonnegative_dispersion"
    elif measurement in {"ratio", "probability"}:
        sign_semantics = "bounded"
    elif concept in {"market_price", "sentiment"}:
        sign_semantics = "signed_level"
    elif concept == "liquidity":
        sign_semantics = "nonnegative_level"
    else:
        sign_semantics = "unknown"

    direction_meaning = {
        "analyst_revision": "information_update",
        "analyst_dispersion": "expectation_dispersion",
        "option_relative": "relative_option_position",
        "option": "option_measurement",
        "volatility": "risk_exposure",
        "event_count": "attention_events",
        "liquidity": "market_participation",
        "sentiment": "attention_or_belief",
        "valuation": "relative_value",
        "earnings": "operating_expectation",
        "fundamental": "economic_scale",
        "market_price": "market_level",
        "data_quality": "data_availability",
    }.get(concept, "unknown")

    tags = set()
    if concept == "market_price" or any(word in text for word in ("price", "close", "high", "low", "vwap")):
        tags.add("price")
    if concept == "liquidity" or any(word in text for word in ("volume", "turnover", "liquidity")):
        tags.add("volume")
    if concept == "volatility":
        tags.add("volatility")
    if concept == "option_relative":
        tags.add("relative")
    if "option" in text or "call" in text or "put" in text:
        tags.add("option")
    if "put" in text:
        tags.add("option_put")
    if "call" in text:
        tags.add("option_call")
    if concept == "analyst_revision" or "analyst" in text:
        tags.add("analyst")
    if measurement == "dispersion":
        tags.add("dispersion")
    if concept == "event_count":
        tags.add("event_count")
    if concept in {"fundamental", "earnings", "valuation"}:
        tags.add("fundamental_scale")
    if any(word in text for word in ("revenue", "sales", "earnings", "eps", "profit", "cash flow")):
        tags.add("earnings")
    if any(word in text for word in ("assets", "equity", "book value", "debt")):
        tags.add("asset_scale")
    if any(word in text for word in ("social", "news", "mention")):
        tags.add("attention")
    semantic_admission = (
        "ALLOW" if direct_concept_hits else
        "REVIEW" if concept != "unknown" else "UNKNOWN"
    )
    metadata_semantics = (
        "AVAILABLE" if str(profile.get("description") or "").strip()
        else "UNKNOWN"
    )
    if metadata_semantics != "AVAILABLE" and semantic_admission == "ALLOW":
        semantic_admission = "REVIEW"
    return {
        "concept": concept,
        "measurement": measurement,
        "behavior": behavior,
        "frequency": frequency or "unknown",
        "sign_semantics": sign_semantics,
        "direction_meaning": direction_meaning,
        "update_style": update_style,
        "tags": sorted(tags),
        "status": "KNOWN" if concept != "unknown" and direct_concept_hits else "UNKNOWN",
        "confidence": "HIGH" if direct_concept_hits else "LOW",
        "semantic_admission": semantic_admission,
        "metadata_semantics": metadata_semantics,
        "evidence": {
            "concept": concept_hits,
            "direct_concept": direct_concept_hits,
            "measurement": measurement_hits,
            "frequency": frequency,
        },
    }


class AlphaFactory:
    """Instantiate templates into non-submitting candidate records."""

    def __init__(self, neutralization="SUBINDUSTRY", registry=None,
                 catalog_path=None, require_private=False):
        self.neutralization = str(neutralization or "SUBINDUSTRY").lower()
        self.registry = registry or (
            AlphaTemplateRegistry.from_private(catalog_path)
            if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
        )
        self.last_feasibility = None
        self.last_budget_audit = {}

    def assess_feasibility(self, hypothesis, fields, operator_reference,
                           *, excluded_expressions=(), probe_id=None,
                           max_combinations=256):
        """Run a bounded control-plane feasibility check before assembly."""
        profiles = [field for field in (fields or []) if (
            isinstance(field, dict)
            and field.get("id") is not None
            and str(field.get("description") or "").strip()
            and str(field.get("semantic_status", "UNKNOWN")).upper() != "UNKNOWN"
        )]
        frequency_counts = {
            "explicit": 0, "inferred": 0, "unknown": 0,
        }
        for profile in profiles:
            source = frequency_evidence(profile)["source"]
            if source == "EXPLICIT_PLATFORM":
                frequency_counts["explicit"] += 1
            elif source == "DESCRIPTION_INFERRED":
                frequency_counts["inferred"] += 1
            else:
                frequency_counts["unknown"] += 1
        templates = list(self.registry.economic_templates())
        excluded = {canonical_expression(value) for value in excluded_expressions
                    if isinstance(value, str) and value.strip()}
        candidate_expressions = set()
        counts = {
            "pair_examined": 0, "triple_examined": 0,
            "relationship_allow": 0, "relationship_review": 0,
            "relationship_unknown": 0, "relationship_incompatible": 0,
            "frequency_incompatible": 0, "template_compatible_count": 0,
            "historical_expression_exclusion_count": 0,
            "candidates_before_dedupe": 0, "candidates_after_dedupe": 0,
            "novel_cross_dataset_relationship_count": 0,
            "proposal_contract_rejection_count": 0,
        }
        candidate_fingerprints = set()
        relationship_fingerprints = set()
        mechanism_families = set()
        semantic_mechanism_fingerprints = set()
        structural_family_fingerprints = set()
        field_concept_fingerprints = {
            str(_derive_field_semantic_traits(profile).get("concept") or "unknown")
            for profile in profiles
        }
        combinations_seen = 0
        for template in templates:
            slots = template.economic_field_count
            if slots < 2:
                continue
            iterator = itertools.combinations(profiles, slots)
            for selected in iterator:
                combinations_seen += 1
                if combinations_seen > max(1, int(max_combinations)):
                    break
                if slots == 2:
                    counts["pair_examined"] += 1
                else:
                    counts["triple_examined"] += 1
                relation = self._relationship_gate(list(selected), template)
                admission = relation["admission"]
                if admission == "ALLOW":
                    counts["relationship_allow"] += 1
                elif admission == "REVIEW":
                    counts["relationship_review"] += 1
                else:
                    counts["relationship_incompatible"] += 1
                frequency_status = relation["frequency_compatibility"]["status"]
                if frequency_status == "INCOMPATIBLE":
                    counts["frequency_incompatible"] += 1
                if admission != "ALLOW":
                    if admission == "REVIEW":
                        counts["relationship_unknown"] += 1
                    continue
                mechanism_families.add(str(template.family))
                selected_traits = [
                    _derive_field_semantic_traits(profile) for profile in selected
                ]
                semantic_mechanism_fingerprints.add(
                    semantic_mechanism_key_from_traits(
                        selected_traits, template.family, relation.get("relationship_type")
                    )
                )
                structural_family_fingerprints.add(str(template.family))
                relationship_fingerprints.add(
                    f"{template.family}:{','.join(sorted(str(p.get('id')) for p in selected))}"
                )
                try:
                    values = {
                        slot: str(profile.get("id"))
                        for slot, profile in zip(template.field_slots, selected)
                    }
                    values["g"] = self.neutralization
                    expression = canonical_expression(template.render(values))
                except (KeyError, ValueError):
                    counts["proposal_contract_rejection_count"] += 1
                    continue
                counts["template_compatible_count"] += 1
                counts["candidates_before_dedupe"] += 1
                if expression in excluded:
                    counts["historical_expression_exclusion_count"] += 1
                    continue
                if expression in candidate_expressions:
                    continue
                candidate_expressions.add(expression)
                if len(candidate_fingerprints) < 64:
                    candidate_fingerprints.add(expression)
                counts["candidates_after_dedupe"] += 1
                datasets = {str(profile.get("dataset")) for profile in selected}
                if len(datasets) > 1:
                    counts["novel_cross_dataset_relationship_count"] += 1
            if combinations_seen > max(1, int(max_combinations)):
                break
        taxonomy = "READY"
        if not profiles:
            taxonomy = "FIELD_SEMANTICS_INSUFFICIENT"
        elif frequency_counts["explicit"] + frequency_counts["inferred"] == 0:
            taxonomy = "FREQUENCY_EVIDENCE_INSUFFICIENT"
        elif counts["frequency_incompatible"] and not counts["relationship_allow"]:
            taxonomy = "FREQUENCY_INCOMPATIBLE"
        elif counts["relationship_review"] and not counts["relationship_allow"]:
            taxonomy = "RELATIONSHIP_REVIEW"
        elif counts["candidates_before_dedupe"] and not counts["candidates_after_dedupe"]:
            taxonomy = "MECHANISM_FAMILY_EXHAUSTED"
        elif not counts["template_compatible_count"]:
            taxonomy = "TEMPLATE_INCOMPATIBLE"
        elif not counts["novel_cross_dataset_relationship_count"]:
            taxonomy = "CROSS_DATASET_FEASIBILITY_ZERO"
        result = {
            "probe_id": str(probe_id or (hypothesis or {}).get("id") or "probe"),
            "field_total": len(fields or []), "semantic_known": len(profiles),
            "explicit_frequency_count": frequency_counts["explicit"],
            "inferred_frequency_count": frequency_counts["inferred"],
            "unknown_frequency_count": frequency_counts["unknown"],
            "dataset_count": len({str(p.get("dataset")) for p in profiles if p.get("dataset") is not None}),
            "mechanism_family": sorted(mechanism_families)[0] if len(mechanism_families) == 1 else "mixed",
            "dataset_route": sorted({str(p.get("dataset")) for p in profiles if p.get("dataset") is not None}),
            "candidate_expression_fingerprints": sorted(candidate_fingerprints),
            "relationship_fingerprints": sorted(relationship_fingerprints)[:64],
            "semantic_mechanism_fingerprints": sorted(semantic_mechanism_fingerprints)[:64],
            "structural_family_fingerprints": sorted(structural_family_fingerprints),
            "field_concept_fingerprints": sorted(field_concept_fingerprints),
            "failure_taxonomy": taxonomy,
            "batch_gate": {
                "feasible": counts["novel_cross_dataset_relationship_count"] > 0,
                "reason": taxonomy,
            },
            **counts,
        }
        self.last_feasibility = result
        return result

    @staticmethod
    def requested(hypothesis):
        """Whether the caller explicitly opted into template-first mode."""
        if not isinstance(hypothesis, dict):
            return False
        return bool(
            hypothesis.get("template_ids")
            or hypothesis.get("template_family")
            or hypothesis.get("template_ref")
        )

    def generate(self, hypothesis, fields, count=6, *, operator_mapping=None,
                 operator_capability=None):
        """Fill a bounded template set from verified field slots.

        Field descriptions and type checks remain the responsibility of the
        normal proposal preflight; this function never guesses them.
        """
        if not isinstance(hypothesis, dict):
            return []
        try:
            limit = max(0, int(count))
        except (TypeError, ValueError):
            return []
        if limit <= 0 or not isinstance(fields, (list, tuple)) or not fields:
            return []
        normalized = []
        normalized_profiles = []
        seen_profile_keys = set()
        for field in fields:
            field_id = field.get("id") if isinstance(field, dict) else field
            if not isinstance(field_id, (str, int)) or not field:
                continue
            field_id = str(field_id)
            dataset = field.get("dataset") if isinstance(field, dict) else None
            dataset = str(dataset) if dataset is not None else None
            profile_key = (dataset, field_id)
            if profile_key in seen_profile_keys:
                continue
            seen_profile_keys.add(profile_key)
            normalized.append(field_id)
            normalized_profiles.append(
                dict(field) if isinstance(field, dict) else {"id": field_id}
            )
        if not normalized:
            return []
        primary = normalized[0]
        secondary = normalized[1] if len(normalized) > 1 else None
        tertiary = normalized[2] if len(normalized) > 2 else None
        ref_input = hypothesis.get("template_ref") or {}
        candidates = []
        seen = set()
        for template in self.registry.select(hypothesis):
            if (template.template_mode == "PARTIAL_OPERATOR"
                    and (operator_mapping is None
                         or not self._operator_mappings(template, operator_capability))):
                continue
            values = {
                "p": primary,
                "data_field": primary,
                "s": secondary,
                "t": tertiary,
                "g": self.neutralization,
            }
            if any(
                slot in template.field_slots and not values.get(slot)
                for slot in template.field_slots
            ):
                continue
            slot_profiles = list(normalized_profiles[:template.economic_field_count])
            if template.economic_field_count > 1:
                relation = self._relationship_gate(slot_profiles, template)
                if relation["admission"] != "ALLOW":
                    continue
            else:
                relation = None
            try:
                expression = template.render(values, operator_mapping)
            except (KeyError, ValueError):
                continue
            identity = canonical_expression(expression)
            if identity in seen:
                continue
            seen.add(identity)
            ref = dict(ref_input)
            ref.setdefault("catalog_id", f"private:{template.template_id}")
            ref.setdefault("skeleton_fingerprint", template.fingerprint)
            ref.setdefault("lifecycle", "runnable")
            ref.setdefault("source", "private_or_synthetic_catalog")
            ref.setdefault("slot_name", "p")
            slot_values = {"p": primary, "data_field": primary,
                           "g": self.neutralization}
            for slot in ("s", "t"):
                if values.get(slot):
                    slot_values[slot] = values[slot]
            relationship_audit = None
            if relation is not None:
                relationship_audit = {
                    "slot_assignment": {
                        slot: values[slot]
                        for slot in template.field_slots
                        if slot in values and values[slot]
                    },
                    "relationship_type": relation["relationship_type"],
                    "relationship_admission": relation["admission"],
                    "relationship_reason": list(relation["reasons"]),
                    "slot_assignment_reason": relation["slot_assignment_reason"],
                    "frequency_compatibility": relation["frequency_compatibility"],
                    "symmetric": relation["symmetric"],
                }
            used_ids = extract_fields(expression, normalized)
            profile_by_id = {}
            for profile in normalized_profiles:
                profile_by_id.setdefault(str(profile.get("id")), profile)
            # Keep references in slot/input order.  ``extract_fields`` is
            # intentionally canonical (length-sorted) for parsing, while a
            # template audit must show which profile filled p/data_field/s/t.
            field_refs = []
            for profile in normalized_profiles:
                field_id = str(profile.get("id"))
                if field_id not in used_ids:
                    continue
                field_refs.append({
                    "id": field_id,
                    "dataset": profile.get("dataset"),
                })
            candidates.append(
                {
                    "expression": expression,
                    "rationale": template.rationale,
                    "mutation": f"template:{template.template_id}",
                    "parent": None,
                    "fields_used": used_ids,
                    "field_refs": field_refs,
                    "template_id": template.template_id,
                    "template_mode": template.template_mode,
                    "template_version": template.version,
                    "template_fingerprint": template.fingerprint,
                    "template_structural_fingerprint": template.structural_fingerprint,
                    "template_mechanism_fingerprint": template.mechanism_fingerprint,
                    "template_role": template.role,
                    "template_operator_count": template.operator_count,
                    "template_field_roles": list(template.field_roles),
                    "template_field_relationship": template.field_relationship,
                    "template_novelty_family": template.novelty_family,
                    "template_allowed_settings_arms": list(template.allowed_settings_arms),
                    "template_allowed_horizon_profiles": [list(profile) for profile in template.allowed_horizon_profiles],
                    "template_catalog_source": "private_or_synthetic_catalog",
                    "template_family": template.family,
                    "template_stage_path": template.stage_path,
                    "template_bindings": dict(slot_values),
                    "template_ref": ref,
                    "template_slots": slot_values,
                    "template_branch_of": template.branch_of,
                    "operator_role": (
                        template.operator_slots[0].role
                        if template.operator_slots else None
                    ),
                    "operator_role_mapping": (
                        {template.operator_slots[0].role: next(iter(operator_mapping.values()))}
                        if template.operator_slots and operator_mapping else {}
                    ),
                    "operator_realization_fingerprint": (
                        template.operator_realization_fingerprint(operator_mapping or {})
                        if template.operator_slots else None
                    ),
                    "operator_capability_fingerprint": (
                        (operator_capability or {}).get("capability_fingerprint")
                        if template.operator_slots and isinstance(operator_capability, dict) else None
                    ),
                    "relationship_audit": relationship_audit,
                    "factory_version": "alpha-factory-v1",
                    "economic_mechanism": self._field_mechanism(
                        normalized_profiles[0],
                        _derive_field_semantic_traits(normalized_profiles[0]),
                        template,
                        relation,
                    ),
                    "direction": template.direction,
                    "direction_transform": {
                        "applied": template.direction_transform == "reverse",
                        "reason": template.economic_mechanism,
                    },
                    "expected_horizon": template.expected_horizon,
                    "falsification": template.falsification,
                    "self_correlation_impact": template.self_correlation_impact,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def catalog(self):
        return self.registry.catalog()

    @staticmethod
    def _operator_mappings(template, reference):
        if template.template_mode != "PARTIAL_OPERATOR":
            return [{}]
        if not isinstance(reference, dict) or not AlphaFactory._live_operator_capability(reference):
            return []
        live = {str(value) for value in reference.get("operators") or []}
        slot = template.operator_slots[0]
        return [{slot.name: operator} for operator in slot.allowed_operators
                if operator in live]

    def operator_coverage(self, available=None):
        """Expose registry coverage without inventing operator usage."""
        return self.registry.operator_coverage(available)

    @staticmethod
    def _profile_dataset(profile):
        value = profile.get("dataset") if isinstance(profile, dict) else None
        return str(value) if value is not None else None

    @classmethod
    def _profile_key(cls, profile):
        if not isinstance(profile, dict):
            return (None, None)
        value = profile.get("id")
        return cls._profile_dataset(profile), str(value) if value is not None else None

    @staticmethod
    def derive_field_semantic_traits(profile):
        """Return a derived semantic view without changing the field profile."""
        return _derive_field_semantic_traits(profile)

    @staticmethod
    def _template_semantic_compatibility(template, profile, traits=None):
        """Score unary template fit; unknown semantics remain explicitly weak."""
        traits = traits or _derive_field_semantic_traits(profile)
        family = template.family
        concept = traits["concept"]
        measurement = traits["measurement"]
        behavior = traits["behavior"]
        frequency = traits["frequency"]
        sign_semantics = traits["sign_semantics"]
        known = traits.get("semantic_admission") == "ALLOW"
        field_type = str(profile.get("type") or "").upper()
        reasons = []
        score = 0

        # Public catalog entries are deliberately synthetic fixtures.  They
        # must remain useful for offline schema/factory tests without being
        # mistaken for private economic evidence; production runtime requires
        # the private catalog before any real execution path is available.
        if template.template_id.startswith("toy_"):
            uses_vector = "vec_avg" in template.expression or "vec_sum" in template.expression
            if uses_vector != (field_type == "VECTOR"):
                return {"admission": "REJECT", "score": -100, "reasons": ["VECTOR 类型不匹配"]}
            return {
                "admission": "ALLOW",
                "score": 1,
                "reasons": ["synthetic template fixture; no economic evidence"],
            }

        vector_family = family.startswith("vector_") or family == "vector_aggregation"
        uses_vector = "vec_avg" in template.expression or "vec_sum" in template.expression
        if uses_vector != (field_type == "VECTOR"):
            return {"admission": "REJECT", "score": -100, "reasons": ["VECTOR 类型不匹配"]}
        if field_type == "VECTOR" and not uses_vector:
            return {"admission": "REJECT", "score": -100, "reasons": ["VECTOR 只能进入向量聚合模板"]}

        if family in {"quality_change", "data_resilient_change", "group_data_repair", "data_quality_penalty", "stale_information"}:
            if concept != "data_quality":
                return {"admission": "REJECT", "score": -30, "reasons": ["模板要求 data_quality 语义"]}
            score += 60
            reasons.append("字段语义明确指向数据质量")
        elif family == "event_trigger":
            if (not known or behavior != "event_driven"
                    or frequency in {"weekly", "monthly", "quarterly", "annual"}):
                return {"admission": "REJECT", "score": -30, "reasons": ["缺少事件驱动语义证据"]}
            score += 65
            reasons.append("字段以事件驱动方式更新")
        elif family in {"risk_adjusted_reversal", "downside_risk"}:
            if concept not in {"volatility", "market_price", "liquidity", "analyst_revision"}:
                if known:
                    return {"admission": "REJECT", "score": -20, "reasons": ["风险模板与字段概念不匹配"]}
            score += 55 if concept == "volatility" else 30
            reasons.append("模板把字段变化解释为风险或异常暴露")
        elif family in {"persistent_level", "momentum", "change", "innovation_surprise", "delayed_confirmation",
                        "accumulated_change", "distribution_regime", "adaptive_scale_change", "trend_residual",
                        "compounding_pressure", "turnover_control", "distributional_change", "group_relative_change",
                        "group_relative_extreme", "group_centered_level", "extreme_location", "extreme_low"}:
            if known and concept == "data_quality":
                return {"admission": "REJECT", "score": -20, "reasons": ["数据质量不是该模板的经济输入"]}
            if concept == "analyst_revision":
                score += 65 if family in {"change", "innovation_surprise", "delayed_confirmation", "persistent_level"} else 45
                reasons.append("分析师修正体现信息更新或扩散过程")
            elif measurement in {"change", "dispersion"}:
                score += 48
                reasons.append("字段提供可观察的变化或离散程度")
            elif measurement == "level" and family in {"persistent_level", "distribution_regime", "group_centered_level"}:
                score += 42
                reasons.append("字段水平适合检验相对状态与持续性")
            else:
                score += 22
                reasons.append("字段可作为有限的时间序列基线")
        elif family in {"robust_cross_section", "rank_level", "zscore_level", "group_neutralized", "cross_sectional_rank",
                        "cross_sectional_standardize"}:
            score += 32
            reasons.append("横截面基线不依赖绝对尺度")

        if concept == "volatility":
            if family in {"risk_adjusted_reversal", "downside_risk"}:
                score += 30
                reasons.append("波动率直接支持风险暴露或风险调整机制")
            elif family == "distribution_regime":
                score += 24
                reasons.append("波动率适合风险状态或 regime 表达")
            elif family in {"relative_spread_change", "relative_ratio",
                            "relative_covariance", "relative_correlation",
                            "generic_multi_field_spread", "generic_multi_field_ratio"}:
                score += 18
                reasons.append("波动率可与价格或另一风险量构成相对关系")
        if concept == "analyst_revision":
            if family in {"change", "persistent_level", "innovation_surprise",
                          "delayed_confirmation"}:
                score += 35
                reasons.append("修正字段直接观测预期更新、持续性或滞后确认")
            elif family == "event_trigger":
                score -= 25
                reasons.append("修正虽是更新事件，但优先测试变化本身而非极端触发")
        if behavior == "slow_moving":
            if family == "event_trigger":
                return {"admission": "REJECT", "score": -30, "reasons": ["慢变字段不默认进入事件触发"]}
            if family in {"persistent_level", "distribution_regime", "group_centered_level"}:
                score += 24
                reasons.append("低频字段更适合检验持久状态或历史 regime")
        if sign_semantics == "nonnegative_level" and family in {
                "reversal", "risk_adjusted_reversal", "downside_risk"}:
            score -= 8
            reasons.append("非负水平字段不把符号方向直接解释为反转")
        if concept == "analyst_revision" and family == "data_quality_penalty":
            return {"admission": "REJECT", "score": -30, "reasons": ["修正字段不能冒充数据质量"]}
        if not known:
            if vector_family or template.field_slots != ("p",):
                return {"admission": "REVIEW", "score": score - 15, "reasons": ["语义 UNKNOWN，仅可审阅"]}
            return {"admission": "REVIEW", "score": score - 10, "reasons": ["语义 UNKNOWN，仅可作语法基线"]}
        if not reasons:
            return {"admission": "REVIEW", "score": 0, "reasons": ["没有足够的经济兼容证据"]}
        return {"admission": "ALLOW", "score": score, "reasons": reasons}

    @classmethod
    def _relationship_labels(cls, left, right):
        """Infer only explicit economic relationships from two field traits."""
        left_tags = set(left.get("tags") or [])
        right_tags = set(right.get("tags") or [])
        left_concept = left.get("concept")
        right_concept = right.get("concept")
        labels = set()
        if left_concept == right_concept and left_concept != "unknown":
            labels.add("same_economic_concept")
        if {left_concept, right_concept} == {"market_price", "liquidity"}:
            labels.add("price_volume")
        if (
            left_concept == right_concept == "volatility"
            and {"option_put", "option_call"}.issubset(left_tags | right_tags)
            and bool(left_tags & {"option_put", "option_call"})
            and bool(right_tags & {"option_put", "option_call"})
        ):
            labels.add("option_pair")
        if ("analyst" in left_tags and "dispersion" in right_tags
                or "analyst" in right_tags and "dispersion" in left_tags):
            labels.add("revision_dispersion")
        if {"price", "volatility"}.issubset(left_tags | right_tags):
            labels.add("complementary_expectations")
        if {"price", "fundamental_scale"}.issubset(left_tags | right_tags):
            labels.add("comparable_scale")
        if {"asset_scale", "earnings"}.issubset(left_tags | right_tags):
            labels.add("numerator_denominator")
        if {left_concept, right_concept} in (
            {"earnings", "valuation"}, {"fundamental", "valuation"},
        ):
            labels.add("numerator_denominator")
        return labels

    @staticmethod
    def _frequency_bucket(value):
        text = str(value or "").lower()
        if any(marker in text for marker in ("intraday", "minute", "hour")):
            return "intraday"
        if any(marker in text for marker in ("daily", "day")):
            return "daily"
        if any(marker in text for marker in ("weekly", "week")):
            return "weekly"
        if any(marker in text for marker in ("monthly", "month")):
            return "monthly"
        if "quarter" in text:
            return "quarterly"
        if any(marker in text for marker in ("annual", "year")):
            return "annual"
        return "unknown"

    @classmethod
    def _frequency_compatibility(cls, traits, family):
        """Classify only obvious frequency conflicts for a relation."""
        buckets = [cls._frequency_bucket(item.get("frequency")) for item in traits]
        if any(bucket == "unknown" for bucket in buckets):
            return {
                "status": "REVIEW",
                "buckets": buckets,
                "reasons": ["frequency evidence is incomplete; relationship needs review"],
            }
        if len(set(buckets)) == 1:
            return {
                "status": "COMPATIBLE",
                "buckets": buckets,
                "reasons": [f"frequency compatible: {buckets[0]}"],
            }
        rank = {
            "intraday": 0, "daily": 1, "weekly": 2,
            "monthly": 3, "quarterly": 4, "annual": 5,
        }
        spread = max(rank[bucket] for bucket in buckets) - min(
            rank[bucket] for bucket in buckets
        )
        direct_dependence = family in {"relative_covariance", "relative_correlation"}
        if direct_dependence and spread >= 2:
            return {
                "status": "INCOMPATIBLE",
                "buckets": buckets,
                "reasons": [
                    "frequency incompatible for direct co-movement: "
                    + " vs ".join(buckets)
                ],
            }
        if direct_dependence and any(
            item.get("concept") == "event_count" for item in traits
        ) and any(
            item.get("concept") in {"fundamental", "earnings", "valuation"}
            for item in traits
        ) and spread >= 1:
            return {
                "status": "INCOMPATIBLE",
                "buckets": buckets,
                "reasons": [
                    "frequency incompatible for event-to-fundamental co-movement: "
                    + " vs ".join(buckets)
                ],
            }
        return {
            "status": "REVIEW",
            "buckets": buckets,
            "reasons": [
                "frequency requires review: " + " vs ".join(buckets)
            ],
        }

    @staticmethod
    def _relationship_type(labels):
        for label in (
            "option_pair", "revision_dispersion", "numerator_denominator",
            "same_economic_concept", "comparable_scale", "price_volume",
            "complementary_expectations",
        ):
            if label in labels:
                return label
        return "unknown"

    @classmethod
    def _relationship_gate(cls, profiles, template):
        """Return an auditable relation decision for pair/triple slots."""
        traits = [_derive_field_semantic_traits(profile) for profile in profiles]
        family = {
            "toy_confirmation": "relative_correlation",
            "toy_relative_change": "relationship_spread",
            "toy_scale_surprise": "relative_ratio",
            "toy_sync_corr": "relative_correlation",
            # Private catalog families declare economic names for provenance;
            # reuse the existing relationship contracts instead of creating a
            # second gate for each catalog family.
            "live-risk-decomposition": "relative_ratio",
            "live-beta-correlation-shift": "relative_correlation",
            "live-skew-scaled-change": "relationship_spread",
            "live-iv-term-structure-shift": "relationship_spread",
            "live-sales-estimate-revision": "relationship_spread",
            "live-eps-forecast-dispersion": "relationship_spread",
            "live-operating-profit-asset-intensity": "relative_ratio",
            "live-cashflow-debt-coverage": "relationship_spread",
            "live-equity-asset-structure": "relationship_spread",
            "live-option-positioning-term-slope": "relationship_spread",
            "live-forward-breakeven-dislocation": "relationship_spread",
            "live-news-novelty-sentiment": "relative_correlation",
            "live-social-attention-sentiment": "relationship_spread",
        }.get(template.family, template.family)
        frequency = cls._frequency_compatibility(traits, family)

        def result(admission, score, labels, relationship_type="unknown",
                   *, symmetric=False, preferred=None, assignment_reason="",
                   evidence_strength="LOW", confirmation_mechanism=None,
                   reasons=()):
            all_reasons = list(reasons) + list(frequency["reasons"])
            return {
                "admission": admission,
                "score": score,
                "evidence_strength": evidence_strength,
                "labels": sorted(labels),
                "relationship_type": relationship_type,
                "reasons": all_reasons,
                "preferred_slot_assignment": preferred or "UNRESOLVED",
                "slot_assignment_reason": assignment_reason,
                "symmetric": bool(symmetric),
                "asymmetric": not bool(symmetric),
                "frequency_compatibility": frequency,
                "confirmation_mechanism": confirmation_mechanism,
            }

        # Synthetic catalog probes may be exercised with generic offline
        # fixture profiles that intentionally lack private semantic evidence.
        # Admit only the narrow, explicitly marked market/fundamental fixture
        # case; real runtime still requires the private catalog and real
        # semantic admission.
        categories = {
            str(profile.get("category") or "").lower()
            for profile in profiles if isinstance(profile, dict)
        }
        if (template.template_id.startswith("toy_")
                and categories <= {"market", "fundamental"}
                and categories
                and all(item.get("semantic_admission") == "REVIEW" for item in traits)
                and frequency["status"] == "COMPATIBLE"):
            return result(
                "ALLOW", 1, set(), "synthetic_fixture", symmetric=True,
                preferred={slot: "EITHER" for slot in ("p", "s", "t")
                           if slot in template.field_slots},
                assignment_reason="offline synthetic fixture relationship",
                evidence_strength="LOW",
                reasons=("synthetic fixture has no private economic evidence",),
            )

        if any(item.get("semantic_admission") != "ALLOW" for item in traits):
            return result(
                "REVIEW", 0, [], reasons=(
                    "至少一个字段语义 UNKNOWN/REVIEW，不能宣称经济关系",
                )
            )

        pair_labels = [
            cls._relationship_labels(left, right)
            for left, right in itertools.combinations(traits, 2)
        ]
        labels = set().union(*pair_labels) if pair_labels else set()

        if len(profiles) >= 3:
            if family != "generic_multi_field_confirmation":
                return result(
                    "REJECT", -30, labels, reasons=(
                        "该模板只支持两个字段，不能把三条槽位压成 pair 关系",
                    )
                )
            concepts = {item.get("concept") for item in traits}
            analyst_confirmation = (
                "analyst_revision" in concepts
                and "analyst_dispersion" in concepts
                and any(
                    item.get("concept") == "sentiment"
                    and "analyst" in (item.get("tags") or [])
                    for item in traits
                )
            )
            if analyst_confirmation and frequency["status"] == "COMPATIBLE":
                return result(
                    "ALLOW", 80, labels,
                    "analyst_expectation_update",
                    symmetric=True,
                    preferred={
                        "data_field": "EITHER", "s": "EITHER", "t": "EITHER",
                    },
                    assignment_reason="三条字段共同表达分析师预期更新、离散与推荐变化",
                    evidence_strength="HIGH",
                    confirmation_mechanism="analyst_expectation_update",
                    reasons=("三条 leg 映射到同一 analyst expectation update mechanism",),
                )
            return result(
                "REVIEW", 0, labels,
                "unknown_confirmation",
                symmetric=True,
                reasons=("pair edges do not prove one shared confirmation mechanism",),
            )

        relationship_type = cls._relationship_type(labels)
        preferred = {"p": "EITHER", "s": "EITHER"}
        symmetric = True
        assignment_reason = "relationship is symmetric under this template contract"
        if frequency["status"] == "INCOMPATIBLE":
            return result(
                "REJECT", -40, labels, relationship_type,
                reasons=("frequency incompatibility blocks this relationship",),
            )
        if family in {"relationship_spread", "relative_spread_change", "generic_multi_field_spread"}:
            if relationship_type == "same_economic_concept":
                if traits[0].get("measurement") != traits[1].get("measurement"):
                    return result(
                        "REJECT", -30, labels, relationship_type,
                        reasons=("same concept has incompatible level/change measurements",),
                    )
            allowed = {"same_economic_concept", "option_pair", "revision_dispersion"}
            if relationship_type not in allowed:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("spread requires comparable quantities or an explicit differential",),
                )
        elif family in {"relative_ratio", "generic_multi_field_ratio"}:
            if relationship_type == "option_pair":
                preferred = {"p": "EITHER", "s": "EITHER"}
                symmetric = True
                assignment_reason = "put/call implied volatility pair is symmetric for ratio testing"
            elif relationship_type == "numerator_denominator":
                if not (
                    traits[0].get("concept") == "earnings"
                    and traits[1].get("concept") == "fundamental"
                ):
                    return result(
                        "REJECT", -35, labels, relationship_type,
                        reasons=("ratio direction is only proven for earnings over assets",),
                    )
                preferred = {"p": "numerator", "s": "denominator"}
                symmetric = False
                assignment_reason = "earnings is the numerator and assets is the scale denominator"
            else:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("ratio requires a directional numerator/denominator or put/call pair",),
                )
        elif family in {"relative_covariance", "relative_correlation"}:
            allowed = {
                "same_economic_concept", "option_pair", "revision_dispersion",
                "price_volume", "complementary_expectations",
            }
            if relationship_type not in allowed:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("co-movement requires a shared or explicitly complementary mechanism",),
                )
        else:
            return result(
                "REJECT", -30, labels, relationship_type,
                reasons=("no relationship contract is defined for this template family",),
            )

        if frequency["status"] == "REVIEW":
            return result(
                "REVIEW", 20, labels, relationship_type,
                symmetric=symmetric,
                preferred=preferred,
                assignment_reason=assignment_reason,
                reasons=("frequency compatibility is REVIEW; auto Factory cannot use it",),
            )
        if family in {"relative_ratio", "generic_multi_field_ratio"}:
            return result(
                "ALLOW", 70 if relationship_type == "numerator_denominator" else 60,
                labels, relationship_type,
                symmetric=symmetric,
                preferred=preferred,
                assignment_reason=assignment_reason,
                evidence_strength="HIGH",
                reasons=("directional ratio contract passed",),
            )
        return result(
            "ALLOW", 60, labels, relationship_type,
            symmetric=True,
            preferred={"p": "EITHER", "s": "EITHER"},
            assignment_reason="relationship is symmetric under this template contract",
            evidence_strength="HIGH" if relationship_type in {"option_pair", "revision_dispersion"} else "MEDIUM",
            reasons=("template-specific relationship contract passed",),
        )

    def rank_compatible_templates(self, profile, templates=None):
        """Rank a small, deterministic view of templates for one field."""
        templates = list(templates or self.registry.economic_templates())
        ranked = []
        for template in templates:
            compatibility = self._template_semantic_compatibility(template, profile)
            if compatibility["admission"] == "REJECT":
                continue
            if template.economic_field_count > 1:
                compatibility = dict(compatibility)
                compatibility["admission"] = "REVIEW"
                compatibility["score"] -= 5
                compatibility["reasons"] = list(compatibility["reasons"]) + [
                    "多字段关系需在实际伴侣字段上复核"
                ]
            ranked.append({"template": template, **compatibility})
        ranked.sort(key=lambda item: (-item["score"], item["template"].template_id))
        return ranked

    @staticmethod
    def _field_mechanism(profile, traits, template, relation=None):
        field_id = str(profile.get("id"))
        if traits.get("semantic_admission") != "ALLOW":
            return (
                f"字段 {field_id} 的语义准入为 {traits.get('semantic_admission', 'UNKNOWN')}；"
                f"当前 profile 只能支持 {template.family} 的语法审阅，不能证明该字段具备该经济机制。"
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
            traits["concept"],
            f"该字段的 {traits['measurement']} 测量与 {template.family} 的有限结构相容",
        )
        mechanism = (
            f"字段 {field_id} 被识别为 {traits['concept']}，测量为 {traits['measurement']}，"
            f"频率为 {traits['frequency']}，符号语义为 {traits['sign_semantics']}，"
            f"行为为 {traits['behavior']}；{fit_reason}。"
            "该机制仍需用独立样本和平台 checks 证伪。"
        )
        if relation and relation.get("labels"):
            mechanism += f" 槽位关系证据为：{', '.join(relation['labels'])}。"
        return mechanism

    def _select_companion_profiles(self, fields, primary, required_count, offset,
                                   template=None):
        """Select distinct, type-compatible companion fields for generic slots.

        When the discovery pool contains multiple datasets, prefer companions
        from another dataset.  If no compatible cross-dataset field exists,
        fall back to the same dataset only when that is the sole viable pool;
        this records a truthful limitation instead of silently pretending the
        batch is cross-dataset.
        """
        if required_count <= 0:
            return []
        primary_key = self._profile_key(primary)
        primary_id = str(primary.get("id"))
        primary_type = str(primary.get("type") or "").upper()
        primary_traits = _derive_field_semantic_traits(primary)
        candidates = []
        for candidate in list(fields[offset + 1:]) + list(fields[:offset]):
            if not isinstance(candidate, dict) or not candidate.get("id"):
                continue
            candidate_id = str(candidate.get("id"))
            if candidate_id == primary_id or self._profile_key(candidate) == primary_key:
                continue
            if not isinstance(candidate.get("description"), str) or not candidate["description"].strip():
                continue
            if str(candidate.get("semantic_status", "UNKNOWN")).upper() == "UNKNOWN":
                continue
            if template is not None:
                candidate_traits = _derive_field_semantic_traits(candidate)
                if template.family == "generic_multi_field_confirmation":
                    analyst_family = {
                        "analyst_revision", "analyst_dispersion", "sentiment",
                    }
                    frequency = self._frequency_compatibility(
                        [primary_traits, candidate_traits], template.family
                    )
                    relation = {
                        "admission": (
                            "ALLOW"
                            if {
                                primary_traits.get("concept"),
                                candidate_traits.get("concept"),
                            } <= analyst_family
                            and frequency["status"] == "COMPATIBLE"
                            else "REJECT"
                        ),
                        "score": 40,
                        "labels": [],
                    }
                else:
                    relation = self._relationship_gate([primary, candidate], template)
                if relation["admission"] != "ALLOW":
                    continue
            candidate_type = str(candidate.get("type") or "").upper()
            if primary_type and candidate_type and candidate_type != primary_type:
                continue
            if any(candidate_id == str(item[1].get("id")) for item in candidates):
                continue
            if template is None:
                relation = {"admission": "REVIEW", "score": 0, "labels": []}
            candidates.append((relation, candidate))
        dataset_ids = {
            self._profile_dataset(item) for item in fields
            if isinstance(item, dict) and item.get("id")
        }
        if len(dataset_ids) > 1:
            cross_dataset = [
                item for item in candidates
                if self._profile_dataset(item[1]) != self._profile_dataset(primary)
            ]
            if cross_dataset:
                candidates = cross_dataset + [
                    item for item in candidates if item not in cross_dataset
                ]
        candidates.sort(
            key=lambda item: (
                0 if item[0].get("admission") == "ALLOW" else 1,
                0 if self._profile_dataset(item[1]) != self._profile_dataset(primary) else 1,
                -int(item[0].get("score", 0)),
                str(item[1].get("id")),
            )
        )
        return [item[1] for item in candidates[:required_count]]

    def screen_optimization_parents(self, parents, *, excluded_expressions=None,
                                    min_sharpe=0.9, min_fitness=0.6,
                                    min_turnover=0.01, max_turnover=0.7):
        """Apply deterministic code gates before Agent semantic selection.

        This gate only examines observable evidence and anti-budget signals.
        The lightweight cloud Alpha feed can prioritize or deduplicate a
        parent, but it cannot become performance evidence by itself.
        """
        if not isinstance(parents, (list, tuple)):
            return []
        try:
            min_sharpe, min_fitness = float(min_sharpe), float(min_fitness)
            min_turnover, max_turnover = float(min_turnover), float(max_turnover)
        except (TypeError, ValueError):
            return []
        excluded = {
            canonical_expression(value)
            for value in (excluded_expressions or [])
            if isinstance(value, str) and value.strip()
        }
        screened = []
        seen = set()
        for parent in parents:
            if not isinstance(parent, dict):
                continue
            if str(parent.get("status") or "").upper() != "DONE":
                continue
            expression = parent.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                continue
            identity = canonical_expression(expression)
            if not identity or identity in seen or identity in excluded:
                continue
            admission = optimization_parent_admission(
                parent, min_sharpe=min_sharpe, min_fitness=min_fitness,
                min_turnover=min_turnover, max_turnover=max_turnover,
            )
            # 只有明确、有限的可修结构 health 失败可以进入优化修复路径；未知
            # health 失败、缺失指标、极低 signal 与非法 turnover 仍 fail closed。
            if not admission["admitted"]:
                continue
            seen.add(identity)
            screened.append(parent)
        return screened

    def optimize_signal_proposals(self, parents, operator_reference,
                                   max_candidates=4, excluded_expressions=None,
                                   min_sharpe=0.9, min_fitness=0.6,
                                   min_turnover=0.01, max_turnover=0.7):
        """Create bounded CHILD proposals from already completed signals.

        This is autonomous optimization, not blind mutation: a parent must be
        DONE, have auditable discovery metadata, meet a configurable signal
        threshold, and stay within turnover bounds.  Each child changes one
        declared variable and still goes through the normal Agent preflight.
        """
        if not isinstance(parents, (list, tuple)) or not isinstance(operator_reference, dict):
            return []
        try:
            limit = max(0, int(max_candidates))
        except (TypeError, ValueError):
            return []
        excluded = {
            canonical_expression(value)
            for value in (excluded_expressions or [])
            if isinstance(value, str) and value.strip()
        }
        allowed = {str(value) for value in (operator_reference.get("operators") or [])}
        try:
            min_sharpe, min_fitness = float(min_sharpe), float(min_fitness)
            min_turnover, max_turnover = float(min_turnover), float(max_turnover)
        except (TypeError, ValueError):
            return []
        parents = self.screen_optimization_parents(
            parents,
            excluded_expressions=excluded_expressions,
            min_sharpe=min_sharpe,
            min_fitness=min_fitness,
            min_turnover=min_turnover,
            max_turnover=max_turnover,
        )
        out = []
        seen_parents = set()
        for parent in parents:
            if len(out) >= limit or not isinstance(parent, dict):
                break
            # Automatic children previously performed generic smoothing and
            # window variants.  They are exactly the low-information tuning
            # loop this factory must stop producing.  A future child must be
            # supplied by the agent with a distinct economic mechanism and a
            # complete proposal contract instead of being invented here.
            if not isinstance(parent.get("child_economic_hypothesis"), dict):
                continue
            base = parent.get("expression")
            if not isinstance(base, str) or not base.strip():
                continue
            identity = canonical_expression(base)
            if identity in seen_parents:
                continue
            seen_parents.add(identity)
            fields = parent.get("fields_used") or []
            datasets = parent.get("datasets") or []
            common = {
                "fields": list(fields),
                "datasets": list(datasets),
                "field_understanding": parent.get("field_understanding"),
                "field_analysis": parent.get("field_analysis"),
                "field_source": parent.get("field_source"),
                "field_hypothesis_basis": parent.get("field_hypothesis_basis"),
                "hypothesis_outcome": parent.get("hypothesis_outcome")
                or parent.get("outcome"),
                "confirmation_status": parent.get("confirmation_status"),
                "mechanism_learning": parent.get("mechanism_learning"),
                "unresolved_question": parent.get("unresolved_question"),
                "next_discriminating_question": parent.get(
                    "next_discriminating_question"
                ),
            }
            if (not fields or not datasets or not common["field_understanding"]
                    or not common["field_analysis"] or not common["field_source"]
                    or not common["field_hypothesis_basis"]):
                continue
            child = parent.get("child_economic_hypothesis") or {}
            if not isinstance(child, dict):
                continue
            child_expression = child.get("expression")
            child_mechanism = child.get("economic_mechanism")
            child_change = child.get("change_type")
            if not all(isinstance(value, str) and value.strip() for value in (
                child_expression, child_mechanism, child_change
            )):
                continue
            if parameter_only_change_reason(base, child_expression):
                continue
            if overfit_expression_reason(child_expression):
                continue
            # 提案契约只接受既有 change_type 词表与 {applied, reason} 方向结构：
            # 非法声明在组装处 fail-closed，不产出必然被 preflight 拒绝的 child。
            if child_change not in CHILD_CHANGE_TYPES:
                continue
            child_transform = child.get("direction_transform")
            if child_transform is not None and not isinstance(child_transform, dict):
                continue
            child_changed_variable = child.get("changed_variable")
            if not isinstance(child_changed_variable, str) or not child_changed_variable.strip():
                child_changed_variable = child_change
            variants = ((child_change, child_expression, child_mechanism),)
            for change_type, expression, rationale in variants:
                if len(out) >= limit:
                    break
                normalized = canonical_expression(expression)
                if normalized in excluded:
                    continue
                actual_ops = list(analyze_expression(expression).operators)
                if not set(actual_ops).issubset(allowed):
                    continue
                proposal = dict(common)
                proposal.update({
                    "expression": expression,
                    "operator_mapping": rationale,
                    "economic_mechanism": child_mechanism,
                    "operator_evidence": {
                        "sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"),
                        "operators": actual_ops,
                        "rationale": rationale,
                    },
                    "experiment_question": child.get(
                        "experiment_question",
                        f"新的经济机制 {change_type} 是否在独立证据上改善净收益与稳定性？",
                    ),
                    "expected_failure_modes": [
                        "平滑过度导致信号衰减或延迟",
                        "优化后换手、相关性或健康检查恶化",
                    ],
                    "tuning_risk": bool(child.get("tuning_risk", False)),
                    "experiment_stage": "CHILD",
                    "change_type": change_type,
                    "parent_expression": base,
                    "parent_id": parent.get("id") or parent.get("proposal_id"),
                    "changed_variable": child_changed_variable,
                    "research_role": "EXPLOIT",
                    "lineage_id": parent.get("lineage_id") or parent.get("hypothesis_id"),
                    "template_id": f"auto_opt_{change_type}",
                    "template_family": "autonomous_optimization",
                    "template_stage_path": "L0:completed signal -> L1:one-variable optimization",
                    "template_ref": {"source": "newwqb_autonomous_optimizer",
                                     "parent": identity},
                    "template_slots": {"parent": base},
                    "rationale": child.get("rationale") or rationale,
                    "direction": parent.get("direction") or "long",
                    "expected_horizon": parent.get("expected_horizon") or "short-term",
                    "falsification": child.get(
                        "falsification",
                        "若独立样本、健康检查或自相关证据恶化，则关闭该优化分支。",
                    ),
                    "direction_transform": child_transform or {
                        "applied": False,
                        "reason": "沿用 parent 的方向，不把方向翻转当作新机制。",
                    },
                    "self_correlation_impact": child.get(
                        "self_correlation_impact",
                        {
                            "expected_effect": "UNKNOWN",
                            "basis": "pre_simulation_structural_forecast",
                            "rationale": "优化前没有平台结算序列，不把结构差异冒充为低自相关。",
                            "admission": "REVIEW",
                        },
                    ),
                })
                decision_payload = parent.get("optimization_decision")
                if isinstance(decision_payload, dict) and decision_payload:
                    proposal["optimization_decision"] = dict(decision_payload)
                if parent.get("optimization_decision_id"):
                    proposal["optimization_decision_id"] = parent["optimization_decision_id"]
                proposal["proposal_origin"] = "agent_optimizer"
                proposal["research_layer"] = "optimization"
                proposal["optimization_source"] = parent.get(
                    "optimization_source", "current_run"
                )
                out.append(proposal)
                excluded.add(normalized)
        return out

    def validation_proposals(self, requests, operator_reference, *,
                             max_candidates=4, excluded_expressions=None):
        """Build bounded ROBUSTNESS proposals from Python-resolved VALIDATE requests.

        The Agent chose *what* to validate; Python only resolves the legal
        candidate value, the settings override and the provenance.  Parameter
        variation is never a new economic mechanism, so every proposal is
        ``ROBUSTNESS`` with a pre-registered ValidationPlan.
        """
        from .validation_report import default_validation_plan

        if not isinstance(requests, (list, tuple)) or not isinstance(operator_reference, dict):
            return []
        try:
            limit = max(0, int(max_candidates))
        except (TypeError, ValueError):
            return []
        excluded = {
            canonical_expression(value)
            for value in (excluded_expressions or [])
            if isinstance(value, str) and value.strip()
        }
        allowed_ops = {
            str(value) for value in (operator_reference.get("operators") or [])
        }
        out = []
        for request in requests or ():
            if len(out) >= limit or not isinstance(request, dict):
                break
            parent = request.get("parent")
            variable = str(request.get("variable") or "")
            if not isinstance(parent, dict) or variable not in VALIDATION_CHANGE_TYPES:
                continue
            base = parent.get("expression")
            if not isinstance(base, str) or not base.strip():
                continue
            expression = request.get("expression") or base
            normalized = canonical_expression(expression)
            if not normalized or normalized in excluded:
                continue
            actual_ops = list(analyze_expression(expression).operators)
            if not set(actual_ops).issubset(allowed_ops):
                continue
            fields = parent.get("fields_used") or []
            datasets = parent.get("datasets") or []
            if not fields or not datasets:
                continue
            if any(not parent.get(key) for key in (
                    "field_understanding", "field_analysis",
                    "field_source", "field_hypothesis_basis")):
                continue
            settings_override = request.get("settings_override")
            expected_override = set() if variable == "template_window" else {variable}
            if (not isinstance(settings_override, dict)
                    or set(settings_override) != expected_override):
                continue
            mechanism = parent.get("economic_mechanism")
            if not isinstance(mechanism, str) or not mechanism.strip():
                continue
            expected_effect = str(request.get("expected_effect") or "").strip()
            falsification = str(request.get("falsification") or "").strip()
            reason = str(request.get("reason") or "").strip()
            if not (expected_effect and falsification and reason):
                continue
            proposal = {
                "expression": expression,
                "fields": list(fields),
                "datasets": list(datasets),
                "field_understanding": parent.get("field_understanding"),
                "field_analysis": parent.get("field_analysis"),
                "field_source": parent.get("field_source"),
                "field_hypothesis_basis": parent.get("field_hypothesis_basis"),
                "operator_mapping": reason,
                "economic_mechanism": mechanism,
                "operator_evidence": {
                    "sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"),
                    "operators": actual_ops,
                    "rationale": reason,
                },
                "experiment_question": expected_effect,
                "expected_failure_modes": [
                    "参数邻域内的改善只是噪声或过拟合",
                    "换手、健康或平台 checks 在新取值下恶化",
                ],
                "tuning_risk": False,
                "experiment_stage": "ROBUSTNESS",
                "change_type": VALIDATION_CHANGE_TYPES[variable],
                "parent_expression": base,
                "parent_id": parent.get("id") or parent.get("proposal_id"),
                "changed_variable": variable,
                "research_role": "VALIDATION",
                "lineage_id": parent.get("lineage_id") or parent.get("hypothesis_id"),
                "template_id": f"validate_{variable}",
                "template_family": "bounded_validation",
                "template_stage_path": "L0:completed signal -> L1:one-variable validation",
                "template_ref": {
                    "source": "bounded_validation",
                    "parent": canonical_expression(base),
                },
                "template_slots": {"parent": base},
                "rationale": reason,
                "direction": parent.get("direction") or "long",
                "expected_horizon": parent.get("expected_horizon") or "short-term",
                "falsification": falsification,
                "direction_transform": parent.get("direction_transform") or {
                    "applied": False,
                    "reason": "沿用 parent 的方向，不把方向翻转当作新机制。",
                },
                "self_correlation_impact": parent.get("self_correlation_impact") or {
                    "expected_effect": "UNKNOWN",
                    "basis": "pre_simulation_structural_forecast",
                    "rationale": "参数验证不改变经济暴露来源，先不假设相关性。",
                    "admission": "REVIEW",
                },
                "settings": dict(settings_override),
                "validation_plan": default_validation_plan(parent),
                "proposal_origin": "agent_optimizer",
                "research_layer": "optimization",
            }
            numeric_variant = request.get("numeric_variant")
            if isinstance(numeric_variant, dict) and numeric_variant:
                proposal["numeric_variant"] = dict(numeric_variant)
            settings_variant = request.get("settings_variant")
            if isinstance(settings_variant, dict) and settings_variant:
                proposal["settings_variant"] = dict(settings_variant)
            decision_payload = request.get("optimization_decision") or parent.get("optimization_decision")
            if isinstance(decision_payload, dict) and decision_payload:
                proposal["optimization_decision"] = dict(decision_payload)
            if request.get("optimization_decision_id"):
                proposal["optimization_decision_id"] = request["optimization_decision_id"]
            out.append(proposal)
            excluded.add(normalized)
        return out

    def assemble_proposals(self, hypothesis, fields, operator_reference,
                           max_candidates=8, excluded_expressions=None):
        """Create auditable EXPLORE proposals from one discovery bundle.

        This is the unattended factory's deterministic AI adapter: it only
        uses non-UNKNOWN discovery profiles and emits a minimal BASELINE per
        field/template.  It does not invent a platform fact; the mechanism is
        explicitly recorded as a test rationale and remains subject to the
        normal production preflight.
        """
        if not isinstance(hypothesis, dict) or not isinstance(fields, list):
            return []
        try:
            limit = max(0, int(max_candidates))
        except (TypeError, ValueError):
            return []
        if limit == 0 or not isinstance(operator_reference, dict):
            return []
        operators = {
            str(operator) for operator in (operator_reference.get("operators") or [])
            if isinstance(operator, (str, int))
        }
        excluded = {
            canonical_expression(value)
            for value in (excluded_expressions or [])
            if isinstance(value, str) and value.strip()
        }
        source_default = hypothesis.get("field_source")
        assembled = []
        used_fields = set()
        economic_mode = str(hypothesis.get("template_mode") or "").lower() == "economic"
        explicit_templates = self.registry.select(hypothesis)
        has_explicit_templates = bool(
            hypothesis.get("template_ids") or hypothesis.get("template_family")
            or (isinstance(hypothesis.get("template_ref"), dict)
                and hypothesis.get("template_ref"))
        )
        template_order = (
            [template.template_id for template in explicit_templates]
            if has_explicit_templates else (
                [template.template_id for template in self.registry.economic_templates()]
                if economic_mode else [
                    template.template_id
                    for template in self.registry.select(
                        {"selection_group": "factory_default"}
                    )
                ]
            )
        )
        if not template_order:
            return []
        template_catalog = [
            self.registry.get(template_id)
            for template_id in template_order
        ]
        template_catalog = [template for template in template_catalog if template]
        if not template_catalog:
            return []
        family_counts = {}
        for offset, profile in enumerate(fields):
            if not isinstance(profile, dict):
                continue
            raw_field_id = profile.get("id")
            if not isinstance(raw_field_id, (str, int)):
                continue
            field_id = str(raw_field_id)
            description = profile.get("description")
            if not field_id or not isinstance(description, str) or not description.strip():
                continue
            if str(profile.get("semantic_status", "UNKNOWN")).upper() == "UNKNOWN":
                continue
            field_type = str(profile.get("type") or "").upper()
            primary_key = self._profile_key(profile)
            if primary_key in used_fields:
                continue
            selected = None
            traits = _derive_field_semantic_traits(profile)
            compatible_templates = []
            for template in template_catalog:
                compatibility = self._template_semantic_compatibility(
                    template, profile, traits
                )
                if compatibility["admission"] != "REJECT":
                    compatible_templates.append((template, compatibility))
            # Explore only the compatible semantic neighborhood.  Sorting by
            # fit first preserves deterministic, field-aware preference while
            # the offset rotates ties and still allows structural exploration.
            compatible_templates.sort(
                key=lambda item: (-item[1]["score"], item[0].template_id)
            )
            for step in range(len(compatible_templates)):
                template, compatibility = compatible_templates[
                    (offset + step) % len(compatible_templates)
                ]
                template_id = template.template_id
                family_cap = 4 if economic_mode else MAX_TEMPLATE_FAMILY_PER_BATCH
                if family_counts.get(template.family, 0) >= family_cap:
                    continue
                companion_slots = [
                    slot for slot in template.companion_field_slots
                ]
                slot_profiles = [profile]
                slot_profiles.extend(self._select_companion_profiles(
                    fields, profile, len(companion_slots), offset, template
                ))
                if len(slot_profiles) != len(companion_slots) + 1:
                    continue
                relation = None
                if companion_slots:
                    relation = self._relationship_gate(slot_profiles, template)
                    if relation["admission"] != "ALLOW":
                        continue
                mappings = self._operator_mappings(template, operator_reference)
                generated = []
                selected_mapping = None
                for mapping in mappings:
                    generated = self.generate(
                        dict(hypothesis, template_ids=[template_id]),
                        slot_profiles,
                        count=1,
                        operator_mapping=mapping,
                        operator_capability=operator_reference,
                    )
                    if generated:
                        selected_mapping = mapping
                        break
                if not generated:
                    continue
                generated_candidate = generated[0]
                generated_expression = generated_candidate["expression"]
                if (
                    field_type != "VECTOR"
                    and {"vec_avg", "vec_sum"}.intersection(
                        analyze_expression(generated_expression).operators
                    )
                ):
                    continue
                if canonical_expression(generated_expression) in excluded:
                    continue
                actual_ops = list(analyze_expression(generated_expression).operators)
                if not set(actual_ops).issubset(operators):
                    continue
                selected = (
                    template, generated_candidate, actual_ops, slot_profiles,
                    compatibility, relation, selected_mapping,
                )
                break
            if selected is None:
                continue
            template, candidate, actual_ops, slot_profiles, compatibility, relation, selected_mapping = selected
            field_source = profile.get("field_source") or source_default
            if not isinstance(field_source, dict):
                field_source = {"kind": "unknown", "path": None, "snapshot_date": None}
            profile_by_id = {
                str(item.get("id")): item
                for item in slot_profiles
                if isinstance(item, dict) and item.get("id") is not None
            }
            profile_by_key = {
                self._profile_key(item): item for item in slot_profiles
                if isinstance(item, dict) and item.get("id") is not None
            }
            used_field_ids = [
                str(item) for item in (candidate.get("fields_used") or [field_id])
                if str(item) in profile_by_id
            ]
            if not used_field_ids:
                continue
            used_profiles = []
            for field_ref in candidate.get("field_refs") or []:
                if not isinstance(field_ref, dict) or not field_ref.get("id"):
                    continue
                key = (
                    str(field_ref.get("dataset")) if field_ref.get("dataset") is not None else None,
                    str(field_ref.get("id")),
                )
                profile = profile_by_key.get(key) or profile_by_id.get(str(field_ref["id"]))
                if profile is not None and profile not in used_profiles:
                    used_profiles.append(profile)
            if not used_profiles:
                used_profiles = [profile_by_id[item] for item in used_field_ids]
            used_field_ids = [str(item.get("id")) for item in used_profiles]
            field_understanding = {
                item: f"基于本轮 discovery 原文：{profile_by_id[item].get('description')}"
                for item in used_field_ids
            }
            field_analysis = {
                item: {
                    "semantic": profile_by_id[item].get("description"),
                    "coverage": profile_by_id[item].get("coverage"),
                    "frequency": profile_by_id[item].get("frequency"),
                    "data_type": profile_by_id[item].get("type"),
                    "semantic_traits": _derive_field_semantic_traits(
                        profile_by_id[item]
                    ),
                }
                for item in used_field_ids
            }
            mechanisms = {
                item: self._field_mechanism(
                    profile_by_id[item],
                    _derive_field_semantic_traits(profile_by_id[item]),
                    template,
                    relation,
                )
                for item in used_field_ids
            }
            field_hypothesis_basis = {
                item: {
                    "description": profile_by_id[item].get("description"),
                    "mechanism": mechanisms[item],
                    "semantic_traits": _derive_field_semantic_traits(
                        profile_by_id[item]
                    ),
                    "independent_increment": "该 BASELINE 只检验这些字段组合的独立增量信息。",
                    "direction": template.direction,
                }
                for item in used_field_ids
            }
            datasets = []
            for item in used_profiles:
                dataset = item.get("dataset")
                if dataset and dataset not in datasets:
                    datasets.append(dataset)
            proposal = {
                "expression": candidate["expression"],
                "fields": used_field_ids,
                "field_refs": list(candidate.get("field_refs") or []),
                "datasets": datasets,
                "field_understanding": field_understanding,
                "field_analysis": field_analysis,
                "field_source": field_source,
                "field_hypothesis_basis": field_hypothesis_basis,
                "economic_mechanism": mechanisms[used_field_ids[0]],
                "semantic_admission": (
                    relation["admission"] if relation else compatibility["admission"]
                ),
                "direction_transform": {
                    **candidate["direction_transform"],
                    "reason": mechanisms[used_field_ids[0]],
                },
                "operator_mapping": candidate["rationale"],
                "operator_evidence": {
                    "sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"),
                    "operators": actual_ops,
                    "rationale": candidate["rationale"],
                },
                "experiment_question": (
                    (
                        "在机制、字段关系、horizon 和 settings 保持不变时，"
                        f"{candidate.get('operator_role') or 'operator'} 使用 "
                        f"{next(iter((candidate.get('operator_role_mapping') or {}).values()), 'REALIZATION')} "
                        "是否改变可复现结果？"
                    )
                    if template.template_mode == "PARTIAL_OPERATOR" else
                    f"字段 {field_id} 的 {template.template_id} 结构是否提供可复现的增量信号？"
                ),
                "operator_contrast_question_key": (
                    f"{template.branch_of or template.template_id}::"
                    f"{candidate.get('operator_role') or 'operator'}"
                    if template.template_mode == "PARTIAL_OPERATOR" else None
                ),
                "expected_failure_modes": [
                    "字段覆盖不足或缺失导致有效持仓减少",
                    "信号集中或换手异常导致健康检查失败",
                ],
                "tuning_risk": False,
                "experiment_stage": "BASELINE",
                "change_type": "baseline",
                "research_role": "EXPLORE",
                "lineage_id": f"{hypothesis.get('id', 'factory')}:field:{field_id}",
                "signal_family": f"{template.family}:{field_id}",
                # Budget arms distinguish an economic template applied to
                # different verified fields.  This permits breadth in the
                # 100-slot factory batch without treating one field's
                # numeric tuning as a new arm.
                "mechanism_family": f"{template.family}:{field_id}",
                "expected_quality": 1.0,
                "information_gain": 1.0,
                "novelty": 1.0,
                "simulation_cost": 1.0,
                "rationale": candidate["rationale"],
                "direction": template.direction,
                "expected_horizon": template.expected_horizon,
                "falsification": template.falsification,
                "self_correlation_impact": {
                    "expected_effect": "UNKNOWN",
                    "basis": "no_live_behavior_series",
                    "rationale": "模拟前没有平台结算值，不把结构差异冒充为低自相关。",
                    "admission": "REVIEW",
                },
            }
            proposal.update({
                key: candidate[key]
                for key in ("mutation", "template_id", "template_mode", "template_family",
                            "template_stage_path", "template_ref", "template_slots",
                            "relationship_audit", "factory_version",
                            "template_version", "template_fingerprint",
                            "template_catalog_source", "template_bindings",
                            "template_structural_fingerprint",
                            "template_mechanism_fingerprint", "template_role",
                            "template_operator_count", "template_field_roles",
                            "template_field_relationship", "template_novelty_family",
                            "template_allowed_settings_arms",
                            "template_allowed_horizon_profiles")
            })
            if template.template_mode == "PARTIAL_OPERATOR":
                slot = template.operator_slots[0]
                proposal.update({
                    "template_mode": "PARTIAL_OPERATOR",
                    "template_branch_of": template.branch_of,
                    "operator_role": slot.role,
                    "operator_role_mapping": {
                        slot.role: next(iter((selected_mapping or {}).values()))
                    },
                    "operator_realization_fingerprint": candidate.get(
                        "operator_realization_fingerprint"
                    ),
                    "operator_capability_fingerprint": candidate.get(
                        "operator_capability_fingerprint"
                    ),
                })
            proposal["proposal_origin"] = "factory"
            realization_proposals = [proposal]
            if template.template_mode == "PARTIAL_OPERATOR":
                for mapping in self._operator_mappings(template, operator_reference):
                    if mapping == selected_mapping:
                        continue
                    alternative_expression = template.render(
                        candidate["template_bindings"], mapping
                    )
                    alternative = dict(proposal)
                    alternative["expression"] = alternative_expression
                    alternative["operator_role_mapping"] = {
                        slot.role: next(iter(mapping.values()))
                    }
                    alternative["operator_realization_fingerprint"] = (
                        template.operator_realization_fingerprint(mapping)
                    )
                    alternative["operator_evidence"] = dict(proposal["operator_evidence"])
                    alternative["operator_evidence"]["operators"] = list(
                        analyze_expression(alternative_expression).operators
                    )
                    realization_proposals.append(alternative)
            for realization in realization_proposals:
                if len(assembled) >= limit:
                    break
                if family_counts.get(template.family, 0) >= family_cap:
                    break
                expression_key = canonical_expression(realization["expression"])
                if expression_key in excluded:
                    continue
                assembled.append(realization)
                excluded.add(expression_key)
                family_counts[template.family] = family_counts.get(template.family, 0) + 1
            used_fields.add(primary_key)
            if len(assembled) >= limit:
                break
        return assembled

    def generate_factory_batch(self, hypothesis, fields, operator_reference,
                               target=100, excluded_expressions=None, seed=None,
                               research_context=None, max_pending_per_arm=1):
        """Generate one large, structurally diverse factory batch.

        The factory owns breadth.  It cycles verified field profiles through
        the bounded economic template catalog; it does not scan arbitrary
        windows, weights, signs, or other numeric parameters.  Optimization
        proposals are never accepted by this API.
        """
        # Do not expose the previous round's derived audit when this call
        # exits before candidate selection (invalid input or an empty pool).
        self.last_budget_audit = {}
        if not self._live_operator_capability(operator_reference):
            self.last_budget_audit = {
                "status": "OPERATOR_CAPABILITY_UNKNOWN",
                "selected_count": 0,
                "shortage_count": 0,
            }
            return []
        try:
            limit = max(0, int(target))
        except (TypeError, ValueError):
            return []
        if limit <= 0 or not isinstance(fields, list):
            return []
        exploration_pool = []
        seen_slot_scopes = set()
        excluded = {
            canonical_expression(value)
            for value in (excluded_expressions or [])
            if isinstance(value, str) and value.strip()
        }

        templates = list(self.registry.economic_templates())
        explicit_template_ids = hypothesis.get("template_ids") or []
        if isinstance(explicit_template_ids, str):
            explicit_template_ids = [explicit_template_ids]
        include_partial = hypothesis.get("include_partial_operator_branches", True) is not False
        if explicit_template_ids:
            requested = set(explicit_template_ids)
            templates = [item for item in templates if item.template_id in requested]
        if not include_partial:
            templates = [template for template in templates
                         if template.template_mode == "CONCRETE"]
        if not templates:
            self.last_budget_audit = {}
            return []
        verified = [
            field for field in fields
            if isinstance(field, dict)
            and isinstance(field.get("id"), (str, int))
            and isinstance(field.get("description"), str)
            and field.get("description", "").strip()
            and str(field.get("semantic_status", "UNKNOWN")).upper() != "UNKNOWN"
        ]
        # Explore field order, but derive template order from each field's
        # semantic compatibility.  Only a small top-ranked pool is explored;
        # the full catalog is never treated as an interchangeable shuffle.
        rng = random.Random(
            str(seed if seed is not None else hypothesis.get("id", "factory"))
        )
        rng.shuffle(verified)
        pool_limit = max(limit, min(limit * 2, FACTORY_BATCH_SIZE * 2))
        for offset, profile in enumerate(verified):
            if len(exploration_pool) >= pool_limit:
                break
            ranked = self.rank_compatible_templates(profile, templates)
            if not ranked:
                continue
            top_score = ranked[0]["score"]
            high_score_pool = [
                item for item in ranked
                if item["score"] == top_score
            ]
            lower_score_pool = [
                item for item in ranked
                if item["score"] < top_score
            ][:7]
            relationship_pool = [
                item for item in ranked
                if item["template"].economic_field_count > 1
            ][:6]
            rng.shuffle(high_score_pool)
            rng.shuffle(lower_score_pool)
            rng.shuffle(relationship_pool)
            pool = []
            for item in high_score_pool + lower_score_pool + relationship_pool:
                if item not in pool:
                    pool.append(item)
            for ranked_template in pool:
                if len(exploration_pool) >= pool_limit:
                    break
                template = ranked_template["template"]
                # Pair templates require a semantically reviewed secondary
                # field.  The normal assemble path remains the single source
                # of proposal metadata and operator evidence.
                companion_slots = [
                    slot for slot in template.companion_field_slots
                ]
                if companion_slots:
                    # Give assemble_proposals the full rotated pool so its
                    # cross-dataset preference is real.  Passing a preselected
                    # same-dataset pair here would make that safety rule
                    # impossible to enforce.
                    slots = [profile] + verified[offset + 1:] + verified[:offset]
                else:
                    slots = [profile]
                generated = self.assemble_proposals(
                    dict(hypothesis, template_mode="economic",
                         template_ids=[template.template_id]),
                    slots, operator_reference, max_candidates=3,
                    excluded_expressions=excluded,
                )
                if not generated:
                    continue
                for proposal in generated:
                    slot_scope = (
                        proposal.get("template_id"),
                        tuple(sorted(str(field) for field in proposal.get("fields", []))),
                        proposal.get("operator_realization_fingerprint"),
                    )
                    if slot_scope in seen_slot_scopes:
                        continue
                    seen_slot_scopes.add(slot_scope)
                    proposal["proposal_origin"] = "factory"
                    proposal["research_layer"] = "exploration"
                    proposal["exploration_objective"] = "signal_discovery"
                    proposal["research_role"] = "EXPLORE"
                    proposal["experiment_stage"] = "BASELINE"
                    exploration_pool.append(proposal)
                    excluded.add(canonical_expression(proposal["expression"]))
                    if len(exploration_pool) >= pool_limit:
                        break
        automatic_mixed_mode = (
            not explicit_template_ids
            and hypothesis.get("include_partial_operator_branches", True) is not False
        )
        if automatic_mixed_mode:
            concrete_expressions = {
                canonical_expression(item["expression"])
                for item in exploration_pool
                if item.get("template_mode") == "CONCRETE"
            }
            exploration_pool = [
                item for item in exploration_pool
                if not (
                    item.get("template_mode") == "PARTIAL_OPERATOR"
                    and canonical_expression(item["expression"]) in concrete_expressions
                )
            ]
        result, self.last_budget_audit = select_budget_candidates(
            exploration_pool, target=limit, context=research_context,
            max_pending_per_arm=max_pending_per_arm, seed=seed,
        )
        return result

    @staticmethod
    def _live_operator_capability(reference):
        """Require current BRAIN evidence before Probe realization."""
        return (
            isinstance(reference, dict)
            and reference.get("status") == "LIVE_VERIFIED"
            and reference.get("availability") == "AVAILABLE"
            and reference.get("source") == "BRAIN_LIVE_ONLY"
            and isinstance(reference.get("operators"), list)
        )
