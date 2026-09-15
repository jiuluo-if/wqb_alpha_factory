"""Rebuildable search state view; no writes and no transport dependencies."""

from collections import Counter, defaultdict

from .schema import CREATED_BY_VERSION, SEARCH_SNAPSHOT_VERSION
from .search_evidence import research_arm_key, structural_fingerprint


class SearchSnapshot(dict):
    """Compact projection of trajectory, ledger and unfinished checkpoint facts."""

    @classmethod
    def from_sources(cls, experiments=(), ledger_summary=None, checkpoint_experiments=()):
        arms = defaultdict(lambda: {
            "admitted": 0, "submitted": 0, "evaluated": 0, "done": 0,
            "failed_research": 0, "failed_infra": 0, "skipped_local": 0,
            "completed": 0, "pending": 0, "running": 0, "unknown": 0,
            "reserved": 0, "reward_sum": 0.0, "reward_count": 0, "reward": 0.0,
        })
        family_counts = Counter()
        structural_counts = Counter()
        validation_proposals = {}
        candidate_count = 0
        simulation_count = 0
        proposal_states = {}

        def value(item, key, default=None):
            return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)

        def arm(item):
            return research_arm_key({
                "datasets": value(item, "datasets") or value(item, "dataset_family"),
                "template_family": value(item, "template_family") or value(item, "mechanism_family"),
            })

        seen_items = set()
        for item in list(experiments or ()) + list(checkpoint_experiments or ()):
            status = str(value(item, "status", "UNKNOWN") or "UNKNOWN").upper()
            key = arm(item)
            proposal_id = str(value(item, "allocation_key") or value(item, "proposal_id") or value(item, "candidate_id") or value(item, "id") or value(item, "expression") or len(seen_items))
            if proposal_id in seen_items:
                continue
            seen_items.add(proposal_id)
            candidate_count += 1
            proposal_states[proposal_id] = {
                "arm": key,
                "status": status,
                "committed": status != "SKIPPED_LOCAL",
            }
            family = str(value(item, "template_family") or "unknown")
            family_counts[family] += 1
            structural = structural_fingerprint(value(item, "expression", ""), value(item, "fields_used") or value(item, "fields") or [])
            structural_counts[structural] += 1
            arms[key]["admitted"] += 1
            if status in {"DONE", "FAILED", "SKIPPED_STALE", "SKIPPED_UNKNOWN", "SKIPPED"}:
                simulation_count += 1
                arms[key]["submitted"] += 1
                if status == "DONE":
                    metrics = value(item, "metrics", {}) or {}
                    try:
                        reward = float(metrics.get("fitness", 0.0))
                        arms[key]["reward"] += reward
                        arms[key]["reward_sum"] += reward
                    except (TypeError, ValueError):
                        pass
                    arms[key]["completed"] += 1
                    arms[key]["done"] += 1
                    arms[key]["evaluated"] += 1
                    arms[key]["reward_count"] += 1
                elif status.startswith("SKIPPED") or status == "FAILED":
                    arms[key]["completed"] += 1
                    if status == "FAILED":
                        arms[key]["failed_research"] += 1
            elif status in {"PENDING", "SUBMITTING"}:
                arms[key]["pending"] += 1
            elif status == "RUNNING":
                arms[key]["running"] += 1
                arms[key]["submitted"] += 1
            else:
                arms[key]["unknown"] += 1
                if status in {"UNKNOWN", "SUBMIT_UNKNOWN"}:
                    arms[key]["submitted"] += 1

        if isinstance(ledger_summary, dict):
            lifecycle_arms = ledger_summary.get("lifecycle_arm_counts") or {}
            lifecycle_proposals = ledger_summary.get("lifecycle_proposals") or {}
            # Explicit Simulation lifecycle evidence outranks trajectory
            # status during recovery.  Candidate-only ledger rows are never
            # projected into allocator occupancy.
            for key, value in lifecycle_arms.items():
                arms[str(key)] = dict(value)
            for proposal_id, value in lifecycle_proposals.items():
                if isinstance(value, dict):
                    proposal_states[str(proposal_id)] = dict(value)
                    if str(value.get("research_role", "")).upper() == "VALIDATION":
                        validation_proposals[str(proposal_id)] = {
                            "status": value.get("status", "RESERVED"),
                            "committed": bool(value.get("committed")),
                        }
            for key, value in (ledger_summary.get("arm_counts") or {}).items():
                if key not in arms and isinstance(value, dict):
                    arms[key].update(value)
            phase_counts = ledger_summary.get("phase_counts") or {}
            # The ledger is the authoritative source for search attempts; it
            # supplements trajectory/checkpoint rows without duplicating them.
            candidate_count = int(ledger_summary.get("candidate_count", candidate_count) or candidate_count)
            simulation_count = max(simulation_count, int(ledger_summary.get("submitted_count", phase_counts.get("submitted", 0)) or 0))
            for family, count in (ledger_summary.get("family_counts") or {}).items():
                family_counts[str(family)] = max(family_counts[str(family)], int(count or 0))
            for structural, count in (ledger_summary.get("structural_family_counts") or {}).items():
                structural_counts[str(structural)] = max(structural_counts[str(structural)], int(count or 0))

        return cls({
            "schema_version": SEARCH_SNAPSHOT_VERSION,
            "created_by_version": CREATED_BY_VERSION,
            "arms": {key: dict(value) for key, value in arms.items()},
            "family_counts": dict(family_counts),
            "structural_family_counts": dict(structural_counts),
            "candidate_count": candidate_count,
            "simulation_count": simulation_count,
            "proposals": proposal_states,
            "validation_proposals": validation_proposals,
            "validation_committed": sum(
                1 for value in validation_proposals.values() if value.get("committed")
            ),
        })

    def allocator_state(self, total_budget=100):
        consumed = sum(1 for value in (self.get("proposals") or {}).values()
                       if isinstance(value, dict) and value.get("committed"))
        return {
            "total_budget": total_budget,
            "consumed_budget": consumed,
            "arms": self.get("arms", {}),
            "proposals": self.get("proposals", {}),
            "family_counts": self.get("family_counts", {}),
            "validation_proposals": self.get("validation_proposals", {}),
            "validation_committed": self.get("validation_committed", 0),
        }
