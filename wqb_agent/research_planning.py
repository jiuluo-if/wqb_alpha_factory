"""Pure research-space and hypothesis planning projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def form_research_space(round_no: int, seed: Mapping[str, object], dataset_pool: Sequence[str]) -> dict[str, object]:
    datasets = list(seed.get("datasets") or seed.get("dataset_hints") or [])
    for dataset_id in dataset_pool:
        if dataset_id not in datasets:
            datasets.append(dataset_id)
    if not datasets:
        datasets = list(dataset_pool)
    return {
        "id": seed.get("id", f"space-r{round_no}"),
        "statement": "Which low-usage, semantically documented fields can test a new mechanism?",
        "tags": list(seed.get("tags") or []),
        "datasets": datasets,
        "parent_best": seed.get("parent_best"),
    }


def iterate_best_hypothesis(round_no: int, best: Mapping[str, object], idea: Mapping[str, object] | None = None) -> dict[str, object]:
    fields = list(best.get("fields_used") or [])
    datasets = list(best.get("datasets") or [])
    tags = ["iterate", "best"]
    if fields:
        tags.append(str(fields[0]))
    if idea:
        statement = f"{idea['idea']} (iterating on current best: {best['expression']})"
        datasets = sorted(set(datasets) | set(idea.get("datasets") or []))
    else:
        statement = (
            f"Iterate on current best {best['expression']} with bounded "
            "single-variable mutations."
        )
    metrics = best.get("metrics") or {}
    sharpe = metrics.get("sharpe")
    return {
        "id": f"h-iter-r{round_no}",
        "statement": statement,
        "tags": tags,
        "direction": "reversal" if sharpe is not None and sharpe < 0 else "long",
        "datasets": datasets,
        "parent_best": best.get("id"),
    }


def select_exploration_seed(round_no: int, hypotheses: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not hypotheses:
        raise ValueError("hypotheses must not be empty")
    return dict(hypotheses[round_no % len(hypotheses)])
