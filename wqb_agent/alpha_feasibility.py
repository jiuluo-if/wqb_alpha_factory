"""Pure, bounded feasibility diagnostics for Alpha candidate assembly."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping

from .alpha_semantics import derive_field_semantic_traits
from .discovery import profile_frequency_evidence
from .diversity import semantic_mechanism_key_from_traits
from .expression import canonical_expression


def assess_feasibility(
    hypothesis: Mapping[str, object] | None,
    fields: Iterable[Mapping[str, object]] | None,
    templates: Iterable[object],
    neutralization: str,
    relationship_gate: Callable[
        [list[Mapping[str, object]], object], Mapping[str, object]
    ],
    *,
    excluded_expressions: Iterable[str] = (),
    probe_id: str | None = None,
    max_combinations: int = 256,
) -> dict[str, object]:
    """Return bounded control-plane feasibility facts without owning state."""
    field_list = list(fields or ())
    profiles = [
        field
        for field in field_list
        if (
            isinstance(field, Mapping)
            and field.get("id") is not None
            and str(field.get("description") or "").strip()
            and str(field.get("semantic_status", "UNKNOWN")).upper() != "UNKNOWN"
        )
    ]
    frequency_counts = {"explicit": 0, "inferred": 0, "unknown": 0}
    frequency_source_counts: dict[str, int] = {}
    frequency_value_counts: dict[str, int] = {}
    for profile in profiles:
        evidence = profile_frequency_evidence(profile)
        source = evidence["source"]
        frequency_source_counts[source] = frequency_source_counts.get(source, 0) + 1
        value = evidence.get("frequency") or "UNKNOWN"
        frequency_value_counts[value] = frequency_value_counts.get(value, 0) + 1
        if source == "EXPLICIT_PLATFORM":
            frequency_counts["explicit"] += 1
        elif source in {
            "DESCRIPTION_INFERRED",
            "DATASET_DESCRIPTION_INFERRED",
            "LEGACY_REDERIVED",
        }:
            frequency_counts["inferred"] += 1
        else:
            frequency_counts["unknown"] += 1

    excluded = {
        canonical_expression(value)
        for value in excluded_expressions
        if isinstance(value, str) and value.strip()
    }
    candidate_expressions: set[str] = set()
    counts = {
        "pair_examined": 0,
        "triple_examined": 0,
        "relationship_allow": 0,
        "relationship_review": 0,
        "relationship_unknown": 0,
        "relationship_incompatible": 0,
        "frequency_incompatible": 0,
        "template_compatible_count": 0,
        "historical_expression_exclusion_count": 0,
        "candidates_before_dedupe": 0,
        "candidates_after_dedupe": 0,
        "novel_cross_dataset_relationship_count": 0,
        "proposal_contract_rejection_count": 0,
    }
    candidate_fingerprints: set[str] = set()
    relationship_fingerprints: set[str] = set()
    mechanism_families: set[str] = set()
    semantic_mechanism_fingerprints: set[str] = set()
    structural_family_fingerprints: set[str] = set()
    field_concept_fingerprints = {
        str(derive_field_semantic_traits(profile).get("concept") or "unknown")
        for profile in profiles
    }
    combinations_seen = 0
    bound = max(1, int(max_combinations))
    for template in templates:
        slots = template.economic_field_count
        if slots < 2:
            continue
        for selected in itertools.combinations(profiles, slots):
            combinations_seen += 1
            if combinations_seen > bound:
                break
            if slots == 2:
                counts["pair_examined"] += 1
            else:
                counts["triple_examined"] += 1
            selected_list = list(selected)
            relation = relationship_gate(selected_list, template)
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
            family = str(template.family)
            mechanism_families.add(family)
            selected_traits = [
                derive_field_semantic_traits(profile) for profile in selected
            ]
            semantic_mechanism_fingerprints.add(
                semantic_mechanism_key_from_traits(
                    selected_traits, family, relation.get("relationship_type")
                )
            )
            structural_family_fingerprints.add(family)
            relationship_fingerprints.add(
                f"{family}:{','.join(sorted(str(p.get('id')) for p in selected))}"
            )
            try:
                values = {
                    slot: str(profile.get("id"))
                    for slot, profile in zip(template.field_slots, selected)
                }
                values["g"] = neutralization
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
            if len({str(profile.get("dataset")) for profile in selected}) > 1:
                counts["novel_cross_dataset_relationship_count"] += 1
        if combinations_seen > bound:
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
        "field_total": len(field_list),
        "semantic_known": len(profiles),
        "explicit_frequency_count": frequency_counts["explicit"],
        "inferred_frequency_count": frequency_counts["inferred"],
        "unknown_frequency_count": frequency_counts["unknown"],
        "frequency_evidence_source_counts": dict(
            sorted(frequency_source_counts.items())
        ),
        "frequency_evidence": ";".join(
            f"{value}:{count}"
            for value, count in sorted(frequency_value_counts.items())
        )
        or "UNKNOWN:0",
        "dataset_count": len(
            {
                str(profile.get("dataset"))
                for profile in profiles
                if profile.get("dataset") is not None
            }
        ),
        "mechanism_family": (
            sorted(mechanism_families)[0] if len(mechanism_families) == 1 else "mixed"
        ),
        "dataset_route": sorted(
            {
                str(profile.get("dataset"))
                for profile in profiles
                if profile.get("dataset") is not None
            }
        ),
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
    return result
