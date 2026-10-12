"""Template-first Alpha candidate factory.

This is a pure candidate generator.  It does not call BRAIN, write research
state, or submit simulations.  A template describes the structural shape of
an Alpha and a field bundle supplies the slots for a SimulationSpec.
"""

import itertools
from collections import deque

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
        relationship_memo = _relationship_memo if _relationship_memo is not None else {}
        expression_memo = _expression_memo if _expression_memo is not None else {}
        iterators = deque(
            self._iter_template_records(
                template, normalized_profiles, normalized,
                operator_mapping=operator_mapping,
                operator_capability=operator_capability,
                relationship_memo=relationship_memo,
                expression_memo=expression_memo,
            )
            for template in self.registry.select(hypothesis)
        )
        candidates = []
        seen = set()
        while iterators and len(candidates) < limit:
            iterator = iterators.popleft()
            while True:
                try:
                    record = next(iterator)
                except StopIteration:
                    break
                identity = record["identity"]
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(record["candidate"])
                iterators.append(iterator)
                break
        return candidates

    def _iter_template_records(self, template, normalized_profiles, normalized, *,
                               operator_mapping=None, operator_capability=None,
                               relationship_memo=None, expression_memo=None):
        """Yield valid records for one template without materializing its space."""
        if template.simulation_type not in {"REGULAR", "REGION_AGNOSTIC"}:
            return
        relationship_memo = relationship_memo if relationship_memo is not None else {}
        expression_memo = expression_memo if expression_memo is not None else {}
        mappings = ([operator_mapping] if operator_mapping is not None
                    else self._operator_mappings(template, operator_capability))
        if not mappings or template.economic_field_count > len(normalized):
            return
        for bundle in self._field_bundles(normalized_profiles, template.economic_field_count):
            slot_profiles = list(bundle)
            if template.economic_field_count == 1:
                semantic = self._template_semantic_compatibility(
                    template, slot_profiles[0]
                )
                if semantic["admission"] != "ALLOW":
                    continue
            else:
                relation = self._relationship_gate_cached(
                    slot_profiles, template, relationship_memo,
                )
                if relation["admission"] != "ALLOW":
                    continue
            values = {"g": self.neutralization}
            for slot, profile in zip(template.field_slots, slot_profiles):
                values[slot] = str(profile.get("id"))
            if "p" in template.field_slots:
                values["data_field"] = values["p"]
            elif "data_field" in template.field_slots:
                values["p"] = values["data_field"]
            for mapping in mappings:
                try:
                    expression = template.render(values, mapping)
                except (KeyError, ValueError):
                    continue
                expression_facts = self._expression_facts(
                    expression, normalized, expression_memo,
                )
                used_ids = expression_facts["fields"]
                field_refs = [
                    {"id": str(profile.get("id")), "dataset": profile.get("dataset")}
                    for profile in normalized_profiles
                    if str(profile.get("id")) in used_ids
                ]
                yield {
                    "identity": expression_facts["identity"],
                    "candidate": {
                        "expression": expression,
                        "rationale": template.economic_mechanism,
                        "field_refs": field_refs,
                        "template_id": template.template_id,
                        "simulation_type": template.simulation_type,
                    },
                }

    def generate(self, hypothesis, fields, count=6, *, operator_mapping=None,
                 operator_capability=None, settings=None):
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
        specs = []
        for item in records:
            simulation_type = str(item.get("simulation_type") or "REGULAR").upper()
            effective_settings = dict(settings or item.get("settings") or {})
            if simulation_type == "REGION_AGNOSTIC":
                effective_settings["region"] = "ALL"
                effective_settings["universe"] = "LARGE"
            specs.append(SimulationSpec(
                expression=item["expression"],
                settings=effective_settings,
                fields=tuple(
                    ref.get("id") for ref in item.get("field_refs", ())
                    if isinstance(ref, dict) and ref.get("id")
                ),
                field_datasets={
                    str(ref["id"]): str(ref["dataset"])
                    for ref in item.get("field_refs", ())
                    if isinstance(ref, dict) and ref.get("id") and ref.get("dataset")
                },
                note=item.get("rationale"),
                template_id=item.get("template_id"),
                simulation_type=simulation_type,
            ))
        return specs

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

    @staticmethod
    def _profile_category(profile):
        value = profile.get("category") if isinstance(profile, dict) else None
        if isinstance(value, dict):
            value = value.get("id") or value.get("name")
        return str(value or "").strip().lower() or None

    @classmethod
    def _profile_key(cls, profile):
        if not isinstance(profile, dict):
            return (None, None)
        value = profile.get("id")
        return cls._profile_dataset(profile), str(value) if value is not None else None

    @staticmethod
    def _field_bundles(profiles, width):
        """Yield ordered, coverage-first bundles without materializing pairs."""
        if width == 1:
            for profile in profiles:
                yield (profile,)
            return
        total = len(profiles)
        seen = set()
        for gap in range(1, total):
            for start in range(total):
                indexes = tuple((start + gap * offset) % total for offset in range(width))
                if len(set(indexes)) != width or indexes in seen:
                    continue
                seen.add(indexes)
                yield tuple(profiles[index] for index in indexes)
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
        # Any ``vec_*`` call is a vector aggregation, not only ``vec_avg`` and
        # ``vec_sum``: matching those two alone rejected legitimate VECTOR
        # templates whose aggregator is e.g. ``vec_max``/``vec_min``, so an
        # explicitly declared VECTOR contract could never be materialized.
        operators = analyze_expression(
            getattr(template, "expression", "") or ""
        ).operators
        uses_vector = any(
            str(operator).startswith("vec_") for operator in operators
        )
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
                   reasons=(), include_frequency_reasons=True):
            all_reasons = list(reasons)
            if include_frequency_reasons:
                all_reasons.extend(frequency["reasons"])
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
        confirmation_mechanism = None
        frequency_scope_alignment = False
        if frequency["status"] == "INCOMPATIBLE":
            return result(
                "REJECT", -40, labels, relationship_type,
                reasons=("frequency incompatibility blocks this relationship",),
            )
        if relationship_contract == "COMPARABLE_SPREAD":
            if relationship_type in {"profitability_efficiency", "beta_correlation"}:
                expected_concepts = {
                    "profitability_efficiency": (
                        "profitability_measure", "asset_efficiency"
                    ),
                    "beta_correlation": ("market_beta", "market_correlation"),
                }[relationship_type]
                expected_categories = {
                    "profitability_efficiency": {"fundamental", "earnings"},
                    "beta_correlation": {"model"},
                }[relationship_type]
                datasets = [cls._profile_dataset(profile) for profile in profiles]
                categories = [cls._profile_category(profile) for profile in profiles]
                field_types = [str(profile.get("type") or "").upper() for profile in profiles]
                if not (
                    [item.get("concept") for item in traits] == list(expected_concepts)
                    and datasets[0]
                    and datasets[0] == datasets[1]
                    and categories[0] == categories[1]
                    and categories[0] in expected_categories
                    and field_types == ["MATRIX", "MATRIX"]
                ):
                    return result(
                        "REJECT", -35, labels, relationship_type,
                        reasons=("specialized spread requires ordered MATRIX roles from one live dataset/category",),
                    )
                assignment_reason = (
                    "profitability is the primary measure and asset efficiency is its same-source comparator"
                    if relationship_type == "profitability_efficiency"
                    else "market beta is the primary exposure and market correlation is its same-window comparator"
                )
                preferred = {
                    "p": expected_concepts[0], "s": expected_concepts[1]
                }
                symmetric = False
                frequency_scope_alignment = (
                    relationship_type == "profitability_efficiency"
                    and set(frequency["buckets"]) == {"unknown"}
                )
            else:
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
                concepts = [item.get("concept") for item in traits]
                dataset_scope = [cls._profile_dataset(profile) for profile in profiles]
                field_types = [str(profile.get("type") or "").upper() for profile in profiles]
                ordinary_ratio = concepts == ["earnings", "fundamental"]
                ordered_asset_ratio = (
                    concepts == ["earnings", "asset_base"]
                    and dataset_scope[0]
                    and dataset_scope[0] == dataset_scope[1]
                    and field_types == ["MATRIX", "MATRIX"]
                    and all(
                        cls._profile_category(profile) == "fundamental"
                        for profile in profiles
                    )
                )
                if not (ordinary_ratio or ordered_asset_ratio):
                    return result(
                        "REJECT", -35, labels, relationship_type,
                        reasons=("ratio direction is only proven for earnings over assets",),
                    )
                preferred = {"p": "numerator", "s": "denominator"}
                symmetric = False
                assignment_reason = "earnings is the numerator and assets is the scale denominator"
                if ordered_asset_ratio:
                    frequency_scope_alignment = set(frequency["buckets"]) == {"unknown"}
            elif relationship_type in {
                "equity_asset_base", "cashflow_debt_coverage",
                "realized_implied_volatility", "sentiment_attention_scale",
            }:
                expected = {
                    "equity_asset_base": (
                        "equity_capital", "asset_base", {"MATRIX"}, {"fundamental"}
                    ),
                    "cashflow_debt_coverage": (
                        "operating_cashflow", "debt_balance", {"MATRIX"},
                        {"fundamental", "earnings"},
                    ),
                    "realized_implied_volatility": (
                        "realized_volatility", "implied_volatility", {"MATRIX"},
                        {"option"},
                    ),
                    "sentiment_attention_scale": (
                        "sentiment", "social_attention", {"MATRIX", "VECTOR"},
                        {"socialmedia"},
                    ),
                }[relationship_type]
                datasets = [cls._profile_dataset(profile) for profile in profiles]
                categories = [cls._profile_category(profile) for profile in profiles]
                field_types = [str(profile.get("type") or "").upper() for profile in profiles]
                if not (
                    [item.get("concept") for item in traits] == list(expected[:2])
                    and datasets[0]
                    and datasets[0] == datasets[1]
                    and categories[0] == categories[1]
                    and categories[0] in expected[3]
                    and all(item in expected[2] for item in field_types)
                    and (
                        relationship_type != "sentiment_attention_scale"
                        or field_types[0] == field_types[1]
                    )
                ):
                    return result(
                        "REJECT", -35, labels, relationship_type,
                        reasons=("specialized ratio requires ordered same-dataset field roles and types",),
                    )
                preferred = {"p": "numerator", "s": "denominator"}
                symmetric = False
                assignment_reason = (
                    "the declared primary measure is the numerator and its same-source scale is the denominator"
                )
                frequency_scope_alignment = set(frequency["buckets"]) == {"unknown"}
            else:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("ratio requires a directional numerator/denominator or put/call pair",),
                )
        elif relationship_contract == "MULTI_FIELD_CONFIRMATION":
            if len(profiles) != 2:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("the two-field confirmation path requires exactly two profiles",),
                )
            datasets = [cls._profile_dataset(profile) for profile in profiles]
            categories = [cls._profile_category(profile) for profile in profiles]
            field_types = [str(profile.get("type") or "").upper() for profile in profiles]
            if relationship_type == "novelty_weighted_sentiment":
                valid_types = (
                    field_types == ["VECTOR", "VECTOR"]
                    and categories == ["news", "news"]
                )
                confirmation_mechanism = "event_novelty_sentiment"
                assignment_reason = (
                    "same-dataset event novelty and sentiment are jointly required by the confirmation contract"
                )
            elif relationship_type == "profitability_efficiency":
                valid_types = (
                    field_types == ["MATRIX", "MATRIX"]
                    and categories[0] == categories[1]
                    and categories[0] in {"fundamental", "earnings"}
                )
                confirmation_mechanism = "profitability_efficiency"
                assignment_reason = (
                    "same-dataset profitability and asset efficiency jointly confirm the fundamental state"
                )
            else:
                valid_types = False
                confirmation_mechanism = None
            if confirmation_mechanism is None:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("two-field confirmation requires one explicitly supported semantic pair",),
                )
            if not datasets[0] or datasets[0] != datasets[1]:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("two-field confirmation requires fields from the same declared dataset",),
                )
            if not valid_types:
                return result(
                    "REJECT", -30, labels, relationship_type,
                    reasons=("the selected confirmation mechanism requires its declared field types",),
                )
            frequency_scope_alignment = set(frequency["buckets"]) == {"unknown"}
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

        if frequency["status"] == "REVIEW" and not frequency_scope_alignment:
            return result(
                "REVIEW", 20, labels, relationship_type,
                symmetric=symmetric,
                preferred=preferred,
                assignment_reason=assignment_reason,
                confirmation_mechanism=confirmation_mechanism,
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
            symmetric=symmetric,
            preferred=preferred,
            assignment_reason=assignment_reason,
            evidence_strength="HIGH" if relationship_type in {"option_pair", "revision_dispersion"} else "MEDIUM",
            confirmation_mechanism=confirmation_mechanism,
            reasons=(
                "same BRAIN dataset and compatible field types establish pair scope; cadence metadata is unavailable",
            ) if frequency_scope_alignment else ("template-specific relationship contract passed",),
            include_frequency_reasons=not frequency_scope_alignment,
        )




    def generate_probe_specs(self, hypothesis, fields, operator_reference,
                             target=100, simulation_settings=None):
        """Generate reviewable executable specs without a proposal envelope."""
        return self.generate(
            hypothesis, fields, count=target,
            operator_capability=operator_reference,
            settings=simulation_settings,
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
