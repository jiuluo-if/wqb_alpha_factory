"""ROLE: INTERNAL
AGENT_RELEVANCE: LOW
PURPOSE: Apply configurable research allocation and diversity heuristics.
READ WHEN: debugging search allocation behavior.
DO NOT USE FOR: treating heuristic policy as a BRAIN invariant.

Pure search policy, diversity evidence, and bounded arm allocation."""

import math
from collections import Counter, defaultdict

from . import search_evidence

_get = search_evidence._get
empirical_pool_summary = search_evidence.empirical_pool_summary
incremental_novelty = search_evidence.incremental_novelty
pareto_front = search_evidence.pareto_front
research_arm_key = search_evidence.research_arm_key
structural_fingerprint = search_evidence.structural_fingerprint
syntax_diversity = search_evidence.syntax_diversity
token_ngram_fingerprints = search_evidence.token_ngram_fingerprints

_ACTIVE = {"RESERVED", "PENDING", "RUNNING", "UNKNOWN"}
_TERMINAL = {"DONE", "FAILED", "SKIPPED", "SKIPPED_LOCAL"}


def validate_budget_hierarchy(*, factory_max_simulations, search_max_simulations,
                              research_max_simulations):
    # MECHANISM_INVARIANT:
    # These numeric caps protect remote budget and may not be relaxed by a
    # research heuristic. Allocation among valid experiments is policy.
    """Fail closed when nested Simulation budgets contradict their scope."""
    values = {
        "factory.max_simulations": factory_max_simulations,
        "search_policy.max_simulations": search_max_simulations,
        "research_allocation.max_simulations": research_max_simulations,
    }
    parsed = {}
    for name, value in values.items():
        try:
            parsed[name] = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是非负整数") from None
        if parsed[name] < 0:
            raise ValueError(f"{name} 必须是非负整数")
    if parsed["search_policy.max_simulations"] > parsed["factory.max_simulations"]:
        raise ValueError("search_policy.max_simulations 不得超过 factory.max_simulations")
    if parsed["research_allocation.max_simulations"] > parsed["search_policy.max_simulations"]:
        raise ValueError("research_allocation.max_simulations 不得超过 search_policy.max_simulations")
    return parsed


class BudgetAllocator:
    """UCB allocator with explicit proposal-level, idempotent lifecycle."""

    def __init__(self, total_budget=100, exploration=1.0, max_pending_per_arm=1):
        self.total_budget = max(0, int(total_budget))
        self.exploration = float(exploration)
        self.max_pending_per_arm = max(1, int(max_pending_per_arm))
        self.arms = defaultdict(lambda: {
            "admitted": 0, "submitted": 0, "evaluated": 0, "done": 0,
            "failed_research": 0, "failed_infra": 0, "skipped_local": 0,
            "completed": 0, "pending": 0, "running": 0, "unknown": 0,
            "reserved": 0, "reward_sum": 0.0, "reward_count": 0,
            "reward": 0.0,
        })
        self.proposals = {}
        self.consumed_budget = 0
        self.last_rejection_code = None

    @staticmethod
    def arm_key(proposal):
        return research_arm_key(proposal)

    @staticmethod
    def proposal_key(proposal):
        if isinstance(proposal, str):
            return proposal
        return str(_get(proposal, "proposal_id") or _get(proposal, "id") or
                   _get(proposal, "submission_fingerprint") or _get(proposal, "expression") or id(proposal))

    def _state(self, arm):
        return self.arms[self.arm_key(arm) if isinstance(arm, dict) else str(arm)]

    def can_reserve(self, proposal):
        self.last_rejection_code = None
        key = self.proposal_key(proposal)
        existing = self.proposals.get(key)
        if existing and existing["status"] in _ACTIVE:
            return True
        if existing and existing["status"] in _TERMINAL:
            current_arm = self.arm_key(proposal)
            if current_arm != existing.get("arm"):
                self.last_rejection_code = "TERMINAL_PROPOSAL_KEY_ARM_REBIND"
                return False
        if self.consumed_budget >= self.total_budget:
            self.last_rejection_code = "SIMULATION_BUDGET"
            return False
        state = self._state(proposal)
        available = state["pending"] + state["running"] + state["unknown"] + state["reserved"] < self.max_pending_per_arm
        if not available:
            self.last_rejection_code = "ARM_ADMISSION"
        return available

    def reserve(self, proposal, *, committed=True):
        """Reserve an arm; legacy callers commit immediately by default."""
        key = self.proposal_key(proposal)
        if key in self.proposals:
            # Reservation is a one-shot admission operation.  Lifecycle
            # replay is handled by transition(), so a second reserve cannot
            # look like a fresh budget grant.
            if self.proposals[key]["status"] not in _TERMINAL:
                self.last_rejection_code = "PROPOSAL_LIFECYCLE_ACTIVE"
                return False
            current_arm = self.arm_key(proposal)
            if current_arm != self.proposals[key].get("arm"):
                self.last_rejection_code = "TERMINAL_PROPOSAL_KEY_ARM_REBIND"
                return False
            if self.consumed_budget >= self.total_budget:
                self.last_rejection_code = "SIMULATION_BUDGET"
                return False
            arm = self.proposals[key]["arm"]
            state = self.arms[arm]
            if state["pending"] + state["running"] + state["unknown"] + state["reserved"] >= self.max_pending_per_arm:
                self.last_rejection_code = "ARM_ADMISSION"
                return False
            state["reserved"] += 1
            self.proposals[key]["status"] = "RESERVED"
            self.proposals[key]["committed"] = bool(committed)
            self.proposals[key].setdefault("submitted", False)
            state["admitted"] += 1
            if committed:
                self.consumed_budget += 1
            return True
        if not self.can_reserve(proposal):
            return False
        arm = self.arm_key(proposal)
        self.arms[arm]["reserved"] += 1
        self.arms[arm]["admitted"] += 1
        self.proposals[key] = {"arm": arm, "status": "RESERVED", "committed": bool(committed), "submitted": False}
        if committed:
            self.consumed_budget += 1
        return True

    def admit(self, proposal):
        """Admit a candidate without consuming Simulation budget."""
        return self.reserve(proposal, committed=False)

    def commit(self, proposal):
        key = self.proposal_key(proposal)
        if key not in self.proposals:
            if not self.admit(proposal):
                return False
        current = self.proposals[key]
        if current.get("committed"):
            return True
        if self.consumed_budget >= self.total_budget:
            return False
        current["committed"] = True
        self.consumed_budget += 1
        return True

    def transition(self, proposal, status, reward=None, *, outcome=None):
        status = str(status or "").upper()
        if status not in _ACTIVE | _TERMINAL:
            raise ValueError(f"未知 allocator 状态: {status}")
        key = self.proposal_key(proposal)
        if key not in self.proposals:
            if not self.reserve(proposal, committed=True):
                return False
        current = self.proposals[key]
        if current["status"] == status:
            return True
        state = self.arms[current["arm"]]
        old = current["status"]
        if status in {"RUNNING", "UNKNOWN"} and not current.get("submitted"):
            state["submitted"] += 1
            current["submitted"] = True
        if old in _ACTIVE:
            state[old.lower()] = max(0, state[old.lower()] - 1)
        if status in _ACTIVE:
            state[status.lower()] += 1
        elif status in _TERMINAL and old not in _TERMINAL:
            state["completed"] += 1
            category = str(outcome or "").upper()
            infrastructure = category in {"INFRA", "RATE_LIMIT", "AUTH", "TIMEOUT", "SUBMIT_UNKNOWN"}
            has_reward = reward is not None and not infrastructure
            if status == "DONE":
                state["done"] += 1
                if has_reward:
                    state["evaluated"] += 1
                    state["reward_count"] += 1
                    value = float(reward)
                    state["reward_sum"] += value
                    state["reward"] += value
                    current["reward"] = value
            elif status == "SKIPPED_LOCAL":
                state["skipped_local"] += 1
            elif status == "FAILED":
                if infrastructure:
                    state["failed_infra"] += 1
                elif category in {"RESEARCH", "FAIL"}:
                    state["failed_research"] += 1
                    # A completed Simulation whose evidence falsifies the
                    # mechanism is a valid zero-reward observation when the
                    # caller explicitly supplies reward=0.0.
                    if has_reward:
                        state["evaluated"] += 1
                        state["reward_count"] += 1
                        value = float(reward)
                        state["reward_sum"] += value
                        state["reward"] += value
                        current["reward"] = value
        current["status"] = status
        return True

    def complete(self, proposal, reward=0.0):
        return self.transition(proposal, "DONE", reward=reward)

    def replace_reward(self, proposal, reward):
        """Replace one observation after final evidence, without recounting it."""
        key = self.proposal_key(proposal)
        current = self.proposals.get(key)
        if not current or current.get("status") != "DONE":
            return False
        try:
            value = float(reward)
        except (TypeError, ValueError):
            return False
        previous = current.get("settled_reward", current.get("reward", 0.0))
        state = self.arms[current["arm"]]
        state["reward_sum"] += value - float(previous or 0.0)
        state["reward"] += value - float(previous or 0.0)
        current["settled_reward"] = value
        current["reward"] = value
        return True

    def mark_pending(self, proposal):
        return self.transition(proposal, "PENDING")

    def mark_unknown(self, proposal):
        return self.transition(proposal, "UNKNOWN")

    def mark_running(self, proposal):
        return self.transition(proposal, "RUNNING")

    def mark_failed(self, proposal, outcome=None, reward=None):
        return self.transition(proposal, "FAILED", reward=reward, outcome=outcome)

    def mark_skipped(self, proposal):
        return self.transition(proposal, "SKIPPED")

    def mark_skipped_local(self, proposal):
        return self.transition(proposal, "SKIPPED_LOCAL")

    def score(self, proposal):
        state = self._state(proposal)
        count = state.get("reward_count", state.get("done", 0))
        total = sum(item.get("reward_count", item.get("done", 0)) for item in self.arms.values())
        if count == 0:
            return self.exploration + (1.0 if total else 0.0)
        return state.get("reward_sum", state.get("reward", 0.0)) / count + self.exploration * math.sqrt(math.log(max(2, total + 1)) / count)

    def snapshot(self):
        return {"total_budget": self.total_budget, "consumed_budget": self.consumed_budget,
                "arms": {key: dict(value) for key, value in self.arms.items()},
                "proposals": {key: dict(value) for key, value in self.proposals.items()}}

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        for key, value in (snapshot.get("arms") or {}).items():
            if isinstance(value, dict):
                self.arms[str(key)].update({field: value.get(field, 0) for field in (
                    "admitted", "submitted", "evaluated", "done", "failed_research", "failed_infra",
                    "skipped_local", "completed", "pending", "running", "unknown", "reserved",
                    "reward_sum", "reward_count", "reward")})
        self.consumed_budget = int(snapshot.get("consumed_budget", sum(
            1 for value in (snapshot.get("proposals") or {}).values()
            if isinstance(value, dict) and value.get("committed")
        )))
        self.proposals.update({str(key): dict(value) for key, value in
                               (snapshot.get("proposals") or {}).items() if isinstance(value, dict)})


class SearchPolicy:
    """Pure facade; state restoration is injected by SearchSnapshot/Agent."""

    def __init__(self, config=None):
        config = config or {}
        self.enabled = bool(config.get("enabled", False))
        self.allocator = BudgetAllocator(config.get("max_simulations", 100), config.get("ucb_exploration", 1.0),
                                          config.get("max_pending_per_arm", 1))
        self.family_counts = Counter()
        self.validation_budget = max(0, int(config.get("validation_max_simulations", 0) or 0))
        self.validation_proposals = {}
        self.validation_committed = 0

    @staticmethod
    def _is_validation(proposal):
        return str(_get(proposal, "research_role", "")).upper() == "VALIDATION"

    def annotate(self, proposal, pool):
        evidence = incremental_novelty(proposal, pool)
        proposal["search_evidence"] = evidence
        proposal["novelty_score"] = evidence["score"]
        return proposal

    def priority(self, proposal):
        if not self.enabled:
            return 0.0
        return float(proposal.get("novelty_score", 0.0)) + self.allocator.score(proposal) - 0.05 * self.family_counts[proposal.get("template_family")]

    def accept(self, proposal):
        if self.enabled and self._is_validation(proposal):
            key = self.allocator.proposal_key(proposal)
            if key in self.validation_proposals:
                return False
            if self.validation_budget and len(self.validation_proposals) >= self.validation_budget:
                return False
            self.validation_proposals[key] = {"status": "RESERVED", "committed": False}
            return True
        accepted = not self.enabled or self.allocator.admit(proposal)
        if accepted and self.enabled:
            self.family_counts[str(proposal.get("template_family") or "unknown")] += 1
        return accepted

    def commit(self, proposal):
        if self.enabled and self._is_validation(proposal):
            key = self.allocator.proposal_key(proposal)
            current = self.validation_proposals.get(key)
            if current is None:
                return False
            if current["committed"]:
                return True
            current["committed"] = True
            self.validation_committed += 1
            return True
        return not self.enabled or self.allocator.commit(proposal)

    def release(self, proposal, status="DONE", reward=None, outcome=None):
        if self.enabled:
            if self._is_validation(proposal):
                key = self.allocator.proposal_key(proposal)
                current = self.validation_proposals.get(key)
                if current is not None:
                    current["status"] = str(status or "DONE").upper()
                return
            if outcome is not None and hasattr(outcome, "reward"):
                reward = outcome.reward
                outcome = "INFRA" if outcome.infrastructure_failure else (
                    "RESEARCH" if outcome.base_quality in {"FAILED", "FAIL"} else None
                )
            if status == "SKIPPED_LOCAL":
                self.allocator.mark_skipped_local(proposal)
            else:
                self.allocator.transition(proposal, status, reward=reward, outcome=outcome)

    def replace_reward(self, proposal, reward):
        if not self.enabled or self._is_validation(proposal):
            return False
        return self.allocator.replace_reward(proposal, reward)

    def snapshot(self):
        snapshot = self.allocator.snapshot()
        snapshot["family_counts"] = dict(self.family_counts)
        snapshot["validation_budget"] = self.validation_budget
        snapshot["validation_proposals"] = {key: dict(value) for key, value in self.validation_proposals.items()}
        snapshot["validation_committed"] = self.validation_committed
        return snapshot

    def restore(self, snapshot):
        self.allocator.restore(snapshot)
        self.family_counts.update({str(key): int(value) for key, value in
                                   (snapshot.get("family_counts") or {}).items()})
        self.validation_budget = int(snapshot.get("validation_budget", self.validation_budget) or 0)
        self.validation_proposals.update({str(key): dict(value) for key, value in
                                          (snapshot.get("validation_proposals") or {}).items()})
        self.validation_committed = int(snapshot.get("validation_committed", sum(
            1 for value in self.validation_proposals.values() if value.get("committed")
        )) or 0)
