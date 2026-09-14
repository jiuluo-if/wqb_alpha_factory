"""Template-first Alpha candidate factory.

This is a proposal-construction layer only.  It does not call BRAIN, write
research state, or submit simulations.  A template describes the structural
shape of an Alpha; a field bundle supplies the slots.  The resulting metadata
keeps the skeleton visible to the later proposal and diversity gates.
"""

import itertools
import os
import random

from .alpha_assembly import assemble_factory_realizations, field_mechanism
from .alpha_feasibility import assess_feasibility as assess_feasibility_projection
from .alpha_relationships import (
    frequency_bucket,
    frequency_compatibility,
    relationship_labels,
    relationship_type,
)
from .alpha_semantics import derive_field_semantic_traits
from .alpha_templates import (
    DEFAULT_TEMPLATES,
    ECONOMIC_TEMPLATES,
    AlphaTemplate,
    AlphaTemplateRegistry,
    TemplateNumericSlot,
    template_numeric_audit,
)
from .alpha_templates.validation import (
    effective_relationship_contract,
    effective_semantic_contract,
    evaluate_semantic_contract,
)
from .diversity import (
    extract_fields,
    select_budget_candidates,
)
from .expression import analyze_expression, canonical_expression
from .field_metadata import profile_frequency_evidence
from .proposal_contract import FACTORY_BATCH_SIZE

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

_derive_field_semantic_traits = derive_field_semantic_traits
class AlphaFactory:
    """Instantiate templates into non-submitting candidate records."""

    def __init__(self, neutralization="SUBINDUSTRY", registry=None,
                 catalog_path=None, require_private=False):
        self.neutralization = str(neutralization or "SUBINDUSTRY").lower()
        self.registry = registry or (
            AlphaTemplateRegistry.from_private(catalog_path)
            if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
        )
        self.catalog_path = catalog_path
        self.last_feasibility = None
        self.last_budget_audit = {}

    def recheck_blocker(self, context):
        """Bounded, read-only control-plane freshness recheck.

        The factory runner calls this once after a blocker recheck is due.
        It only asks whether the upstream control-plane evidence that a
        budget/feasibility blocker depends on (the private template catalog
        that owns relationship contracts, plus the field cache) has moved
        past the recorded blocker's ``last_seen_at``.  It never POSTs, never
        reassembles candidates, and never writes state: ``changed=True``
        simply permits the runner to retry the probe once.

        A route-level ``BUDGET_SHORTAGE`` is not evidence freshness.  The
        runner owns an explicit route-episode advance for that case; this
        hook must not relabel a cooldown expiry as changed evidence.
        """
        context = context if isinstance(context, dict) else {}
        try:
            last_seen = float(context.get("last_seen_at"))
        except (TypeError, ValueError):
            last_seen = 0.0
        if context.get("kind") == "BUDGET_SHORTAGE":
            return {"changed": False, "probe": {}}
        if last_seen <= 0:
            return {"changed": False, "probe": {}}
        paths = []
        if self.catalog_path:
            paths.append(str(self.catalog_path))
        field_cache = context.get("field_cache_path")
        if field_cache:
            paths.append(str(field_cache))
        for path in paths:
            try:
                if os.path.getmtime(path) > last_seen:
                    return {"changed": True, "probe": {}}
            except OSError:
                continue
        return {"changed": False, "probe": {}}

    def assess_feasibility(self, hypothesis, fields, operator_reference,
                           *, excluded_expressions=(), probe_id=None,
                           max_combinations=256):
        """Run a bounded control-plane feasibility check before assembly."""
        result = assess_feasibility_projection(
            hypothesis,
            fields,
            self.registry.economic_templates(),
            self.neutralization,
            self._relationship_gate,
            excluded_expressions=excluded_expressions,
            probe_id=probe_id,
            max_combinations=max_combinations,
        )
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
                 operator_capability=None, _prepared_facts=None,
                 _relationship_memo=None, _expression_memo=None):
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
                relation = self._relationship_gate_cached(
                    slot_profiles, template,
                    _relationship_memo if _relationship_memo is not None else {},
                )
                if relation["admission"] != "ALLOW":
                    continue
            else:
                relation = None
            try:
                expression = template.render(values, operator_mapping)
            except (KeyError, ValueError):
                continue
            expression_facts = self._expression_facts(
                expression, normalized, _expression_memo
            )
            identity = expression_facts["identity"]
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
            used_ids = expression_facts["fields"]
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
                    "relationship_contract": effective_relationship_contract(template),
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
                        self._prepared_traits(normalized_profiles[0], _prepared_facts),
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
    def _prepare_batch_facts(fields):
        """Prepare derived facts for one factory batch only."""
        facts = {}
        for profile in fields or []:
            if not isinstance(profile, dict) or profile.get("id") is None:
                continue
            key = AlphaFactory._profile_key(profile)
            if key in facts:
                continue
            facts[key] = {
                "profile": profile,
                "traits": _derive_field_semantic_traits(profile),
                "field_type": str(profile.get("type") or "").upper(),
            }
        return facts

    @classmethod
    def _prepared_traits(cls, profile, prepared_facts):
        if prepared_facts is not None:
            fact = prepared_facts.get(cls._profile_key(profile))
            if fact is not None:
                return fact["traits"]
        return _derive_field_semantic_traits(profile)

    @staticmethod
    def _expression_facts(expression, normalized, expression_memo=None):
        if expression_memo is not None and expression in expression_memo:
            return expression_memo[expression]
        facts = {
            "identity": canonical_expression(expression),
            "fields": extract_fields(expression, normalized),
            "analysis": analyze_expression(expression),
        }
        if expression_memo is not None:
            expression_memo[expression] = facts
        return facts

    def _relationship_gate_cached(self, profiles, template, relationship_memo):
        """Memoize only within one call, preserving ordered slot semantics."""
        key = (
            tuple(self._profile_key(profile) for profile in profiles),
            effective_relationship_contract(template),
            tuple(str(profile.get("type") or "").upper() for profile in profiles),
        )
        if key not in relationship_memo:
            relationship_memo[key] = self._relationship_gate(profiles, template)
        return relationship_memo[key]

    @staticmethod
    def derive_field_semantic_traits(profile):
        """Return a derived semantic view without changing the field profile."""
        return derive_field_semantic_traits(profile)

    @staticmethod
    def _template_semantic_compatibility(template, profile, traits=None):
        """Score unary template fit; unknown semantics remain explicitly weak."""
        traits = traits or _derive_field_semantic_traits(profile)
        field_type = str(profile.get("type") or "").upper()
        uses_vector = "vec_avg" in template.expression or "vec_sum" in template.expression
        return evaluate_semantic_contract(
            effective_semantic_contract(template), traits,
            field_type=field_type, uses_vector_operator=uses_vector,
            field_slots=template.field_slots,
        )

    @classmethod
    def _relationship_labels(cls, left, right):
        """Infer only explicit economic relationships from two field traits."""
        return relationship_labels(left, right)

    @staticmethod
    def _frequency_bucket(value):
        return frequency_bucket(value)

    @classmethod
    def _frequency_compatibility(cls, traits, relationship_contract):
        """Classify only obvious frequency conflicts for a relation."""
        return frequency_compatibility(traits, relationship_contract)

    @staticmethod
    def _relationship_type(labels):
        return relationship_type(labels)

    @classmethod
    def _relationship_gate(cls, profiles, template):
        """Return an auditable relation decision for pair/triple slots."""
        traits = [_derive_field_semantic_traits(profile) for profile in profiles]
        relationship_contract = effective_relationship_contract(template)
        frequency = cls._frequency_compatibility(traits, relationship_contract)

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
                "contract": relationship_contract,
            }

        if relationship_contract == "UNDECLARED" and len(profiles) > 1:
            return result(
                "REVIEW", 0, [], reasons=(
                    "RELATIONSHIP_CONTRACT_UNDECLARED",
                )
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
            if relationship_contract != "MULTI_FIELD_CONFIRMATION":
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
        if relationship_contract == "COMPARABLE_SPREAD":
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
        elif relationship_contract == "DIRECTIONAL_RATIO":
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
        elif relationship_contract == "CO_MOVEMENT":
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
        if relationship_contract == "DIRECTIONAL_RATIO":
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

    def rank_compatible_templates(self, profile, templates=None, traits=None):
        """Rank a small, deterministic view of templates for one field."""
        templates = list(templates or self.registry.economic_templates())
        ranked = []
        for template in templates:
            compatibility = self._template_semantic_compatibility(template, profile, traits)
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
        return field_mechanism(
            profile,
            traits,
            {"family": template.family},
            relation,
        )

    def _select_companion_profiles(self, fields, primary, required_count, offset,
                                   template=None, *, prepared_facts=None,
                                   relationship_memo=None):
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
        primary_traits = self._prepared_traits(primary, prepared_facts)
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
                candidate_traits = self._prepared_traits(candidate, prepared_facts)
                if effective_relationship_contract(template) == "MULTI_FIELD_CONFIRMATION":
                    analyst_family = {
                        "analyst_revision", "analyst_dispersion", "sentiment",
                    }
                    frequency = self._frequency_compatibility(
                        [primary_traits, candidate_traits],
                        effective_relationship_contract(template),
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
                    relation = self._relationship_gate_cached(
                        [primary, candidate], template,
                        relationship_memo if relationship_memo is not None else {},
                    )
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
        from .optimization_screening import screen_optimization_parents
        return screen_optimization_parents(
            parents, excluded_expressions=excluded_expressions,
            min_sharpe=min_sharpe, min_fitness=min_fitness,
            min_turnover=min_turnover, max_turnover=max_turnover,
        )

    def optimize_signal_proposals(self, parents, operator_reference,
                                  max_candidates=4, excluded_expressions=None,
                                  min_sharpe=0.9, min_fitness=0.6,
                                  min_turnover=0.01, max_turnover=0.7):
        from .optimization_screening import build_optimization_proposals
        return build_optimization_proposals(
            parents, operator_reference, max_candidates=max_candidates,
            excluded_expressions=excluded_expressions,
            min_sharpe=min_sharpe, min_fitness=min_fitness,
            min_turnover=min_turnover, max_turnover=max_turnover,
        )

    def validation_proposals(self, requests, operator_reference, *,
                             max_candidates=4, excluded_expressions=None):
        from .validation_proposals import build_validation_proposals
        return build_validation_proposals(
            requests, operator_reference, max_candidates=max_candidates,
            excluded_expressions=excluded_expressions,
        )

    def assemble_proposals(self, hypothesis, fields, operator_reference,
                           max_candidates=8, excluded_expressions=None,
                           _prepared_facts=None, _relationship_memo=None,
                           _expression_memo=None):
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
        prepared_facts = (
            _prepared_facts
            if _prepared_facts is not None else self._prepare_batch_facts(fields)
        )
        relationship_memo = (
            _relationship_memo if _relationship_memo is not None else {}
        )
        expression_memo = (
            _expression_memo if _expression_memo is not None else {}
        )
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
            traits = self._prepared_traits(profile, prepared_facts)
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
                    fields, profile, len(companion_slots), offset, template,
                    prepared_facts=prepared_facts,
                    relationship_memo=relationship_memo,
                ))
                if len(slot_profiles) != len(companion_slots) + 1:
                    continue
                relation = None
                if companion_slots:
                    relation = self._relationship_gate_cached(
                        slot_profiles, template, relationship_memo
                    )
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
                        _prepared_facts=prepared_facts,
                        _relationship_memo=relationship_memo,
                        _expression_memo=expression_memo,
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
                        self._expression_facts(
                            generated_expression, fields, expression_memo
                        )["analysis"].operators
                    )
                ):
                    continue
                generated_facts = self._expression_facts(
                    generated_expression, fields, expression_memo
                )
                if generated_facts["identity"] in excluded:
                    continue
                actual_ops = list(generated_facts["analysis"].operators)
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
            realization_proposals = assemble_factory_realizations(
                hypothesis=hypothesis, field_id=field_id, profile=profile,
                slot_profiles=slot_profiles, candidate=candidate, actual_ops=actual_ops,
                template=template, compatibility=compatibility, relation=relation,
                selected_mapping=selected_mapping, operator_reference=operator_reference,
                field_source=field_source, field_mechanism_fn=self._field_mechanism,
                derive_traits_fn=_derive_field_semantic_traits,
                frequency_evidence_fn=profile_frequency_evidence,
                operator_mappings_fn=self._operator_mappings,
            )
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
        prepared_facts = self._prepare_batch_facts(verified)
        relationship_memo = {}
        expression_memo = {}
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
            ranked = self.rank_compatible_templates(
                profile, templates,
                prepared_facts[self._profile_key(profile)]["traits"],
            )
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
                    _prepared_facts=prepared_facts,
                    _relationship_memo=relationship_memo,
                    _expression_memo=expression_memo,
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
