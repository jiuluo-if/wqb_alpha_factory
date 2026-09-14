"""Pure route-episode decisions for the legacy factory control plane."""

from __future__ import annotations

from collections.abc import Mapping

ROUTE_DIMENSIONS = {
    "candidate_expression": "candidate_expression_fingerprints",
    "semantic_mechanism": "semantic_mechanism_fingerprints",
    "relationship": "relationship_fingerprints",
    "dataset_route": "dataset_route",
    "research_question": "research_question_fingerprints",
}


def route_decision(
    previous_probe: Mapping[str, object] | None,
    current_probe: Mapping[str, object] | None,
    *,
    route_attempt: int,
    no_gain_attempts: int,
    max_route_attempts: int = 3,
    max_no_gain_attempts: int = 2,
) -> dict[str, object]:
    previous_probe = previous_probe if isinstance(previous_probe, Mapping) else {}
    current_probe = current_probe if isinstance(current_probe, Mapping) else {}
    current_taxonomy = str(current_probe.get("failure_taxonomy") or "UNKNOWN")

    def dimension_changed(dimension):
        digest_key = f"{dimension}_set_digest"
        count_key = f"{dimension}_count"
        if digest_key in previous_probe or digest_key in current_probe:
            if digest_key not in previous_probe and not current_probe.get(count_key):
                return False
            if digest_key not in current_probe and not previous_probe.get(count_key):
                return False
            return (previous_probe.get(digest_key), previous_probe.get(count_key)) != (
                current_probe.get(digest_key),
                current_probe.get(count_key),
            )
        key = ROUTE_DIMENSIONS[dimension]
        return set(previous_probe.get(key) or ()) != set(current_probe.get(key) or ())

    candidate_changed = dimension_changed("candidate_expression")
    new_metadata = any(
        f"{dimension}_set_digest" in previous_probe
        or f"{dimension}_set_digest" in current_probe
        or ROUTE_DIMENSIONS[dimension] in previous_probe
        or ROUTE_DIMENSIONS[dimension] in current_probe
        for dimension in ROUTE_DIMENSIONS
    )
    changes = []
    for name, key in (
        ("semantic_change", "semantic_mechanism_fingerprints"),
        ("relationship_change", "relationship_fingerprints"),
        ("dataset_change", "dataset_route"),
        ("question_change", "research_question_fingerprints"),
    ):
        dimension = {
            "semantic_mechanism_fingerprints": "semantic_mechanism",
            "relationship_fingerprints": "relationship",
            "dataset_route": "dataset_route",
            "research_question_fingerprints": "research_question",
        }[key]
        if dimension_changed(dimension):
            changes.append(name)
    for key in (
        "failure_taxonomy",
        "frequency_evidence",
        "capability_status",
        "rejection_reason_counts",
        "budget_shortage_reason",
        "budget_shortage_count",
    ):
        if (
            key in previous_probe
            and key in current_probe
            and current_probe.get(key) != previous_probe.get(key)
        ):
            changes.append(key)
    if candidate_changed:
        changes.insert(0, "candidate_change")
    information_gain = bool(
        any(
            item in changes
            for item in (
                "semantic_change",
                "relationship_change",
                "dataset_change",
                "question_change",
                "failure_taxonomy",
                "frequency_evidence",
                "capability_status",
                "rejection_reason_counts",
                "budget_shortage_reason",
                "budget_shortage_count",
            )
        )
        if new_metadata
        else changes
    )
    next_no_gain = 0 if information_gain else int(no_gain_attempts) + 1
    if int(route_attempt) >= int(max_route_attempts):
        action, reason = "STOP", "ROUTE_ATTEMPTS_EXHAUSTED"
    elif not information_gain and next_no_gain >= int(max_no_gain_attempts):
        action, reason = "STOP", "NO_INFORMATION_GAIN"
    else:
        action, reason = "REROUTE", current_taxonomy
    route_names = (
        "current_bundle",
        "same_dataset_relationship",
        "same_dataset_new_mechanism",
        "new_dataset_composition",
        "rediscovery",
    )
    return {
        "action": action,
        "reason": reason,
        "information_gain": information_gain,
        "information_changes": changes,
        "change_type": "none"
        if not changes
        else "candidate_change_only"
        if changes == ["candidate_change"]
        else "research_information_change",
        "no_gain_attempts": next_no_gain,
        "route_attempt": int(route_attempt),
        "route_index": min(int(route_attempt) + 1, 4),
        "route_name": route_names[min(int(route_attempt), 4)],
    }
