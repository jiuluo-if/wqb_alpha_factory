"""Pure bounded admission summaries for proposal execution."""

from __future__ import annotations


def rejection_reason_counts(
    rejected, skipped, diversity_rejected, settings_rejected, budget_rejected
):
    return {
        key: count
        for key, count in {
            "PREFLIGHT_REJECTED": len(rejected),
            "DUPLICATE_LOCAL": len(skipped),
            "DIVERSITY_REJECTED": len(diversity_rejected),
            "INVALID_SETTINGS": len(settings_rejected),
            "BATCH_CAP": len(budget_rejected),
        }.items()
        if count
    }
