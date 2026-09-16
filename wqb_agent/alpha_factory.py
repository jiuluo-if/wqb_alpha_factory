"""Template-first Alpha candidate factory.

This is a pure candidate generator.  It does not call BRAIN, write research
state, or submit simulations.  A template describes the structural shape of
an Alpha and a field bundle supplies the slots for a SimulationSpec.
"""

import itertools

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
from .expression import analyze_expression, canonical_expression

__all__ = [
    "AlphaFactory", "AlphaTemplate", "AlphaTemplateRegistry",
    "TemplateNumericSlot", "DEFAULT_TEMPLATES", "ECONOMIC_TEMPLATES",
    "template_numeric_audit",
]


_derive_field_semantic_traits = derive_field_semantic_traits


def _extract_fields(expression, known_fields):
    """Return the known fields that actually occur in an expression."""
    fields = [
        str(field) for field in (known_fields or [])
        if isinstance(field, (str, int)) and str(field)
    ]
    found = set(analyze_expression(expression, fields).fields)
    return [field for field in sorted(set(fields), key=len, reverse=True) if field in found]


class AlphaFactory:
    """Instantiate templates into non-submitting SimulationSpec values."""

    def __init__(self, neutralization="SUBINDUSTRY", registry=None,
                 catalog_path=None, require_private=False):
        self.neutralization = str(neutralization or "SUBINDUSTRY").lower()
        self.registry = registry or (
            AlphaTemplateRegistry.from_private(catalog_path)
            if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
        )
        self.catalog_path = catalog_path

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

    def _generate_records(self, hypothesis, fields, count=6, *, operator_mapping=None,
                 operator_capability=None, _prepared_facts=None,
                 _relationship_memo=None, _expression_memo=None):
        """Fill a bounded template set from verified field slots.

        Field descriptions and type checks remain the responsibility of the
        execution gateway; this function only renders candidate specs.
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
            used_ids = expression_facts["fields"]
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
                    "rationale": template.economic_mechanism,
                    "field_refs": field_refs,
                    "template_id": template.template_id,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def generate(self, hypothesis, fields, count=6, *, operator_mapping=None,
                 operator_capability=None):
        """Return pure executable SimulationSpec values.

        Candidate metadata stays private to the template implementation; the
        public factory surface does not expose proposal envelopes or research
        state fields.
        """
        from .simulation_gateway import SimulationSpec

        records = self._generate_records(
            hypothesis, fields, count=count,
            operator_mapping=operator_mapping,
            operator_capability=operator_capability,
        )
        return [SimulationSpec(
            expression=item["expression"],
            settings=item.get("settings") or {},
            fields=tuple(
                ref.get("id") for ref in item.get("field_refs", ())
                if isinstance(ref, dict) and ref.get("id")
            ),
            note=item.get("rationale"),
            template_id=item.get("template_id"),
        ) for item in records]

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
    def _expression_facts(expression, normalized, expression_memo=None):
        if expression_memo is not None and expression in expression_memo:
            return expression_memo[expression]
        facts = {
            "identity": canonical_expression(expression),
            "fields": _extract_fields(expression, normalized),
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




    def generate_probe_specs(self, hypothesis, fields, operator_reference,
                             target=100, excluded_expressions=None, seed=None,
                             research_context=None, max_pending_per_arm=1):
        """Generate reviewable executable specs without a proposal envelope."""
        del excluded_expressions, seed, research_context, max_pending_per_arm
        return self.generate(
            hypothesis, fields, count=target,
            operator_capability=operator_reference,
        )

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
