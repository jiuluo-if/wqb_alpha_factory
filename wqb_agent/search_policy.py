"""ROLE: INTERNAL
AGENT_RELEVANCE: LOW
PURPOSE: Apply configurable research allocation and diversity heuristics.
READ WHEN: debugging search allocation behavior.
DO NOT USE FOR: treating heuristic policy as a BRAIN invariant.

Pure search policy, diversity evidence, and bounded arm allocation."""

import math
import re
from collections import Counter, defaultdict

from .evidence_status import annotate_evidence

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s]")
_OPERATORS = {
    "rank", "zscore", "ts_mean", "ts_sum", "ts_std_dev", "ts_rank",
    "ts_zscore", "ts_delta", "ts_decay_linear", "delta", "delay",
    "group_neutralize", "group_rank", "scale", "log", "abs", "sign",
    "sqrt", "min", "max", "add", "sub", "mul", "div", "and", "or",
}
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


def _get(record, key, default=None):
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _field_ids(record):
    values = _get(record, "fields_used") or _get(record, "fields") or []
    result = []
    for value in values:
        value = value.get("id") if isinstance(value, dict) else value
        if isinstance(value, (str, int)) and str(value):
            result.append(str(value).lower())
    return result


def research_arm_key(proposal):
    """Return the sole economic arm identity, excluding operator realization."""
    dataset = _get(proposal, "dataset_family") or _get(proposal, "datasets") or "unknown-dataset"
    mechanism = _get(proposal, "mechanism_family") or _get(proposal, "template_family") or "unknown-mechanism"
    if isinstance(dataset, (list, tuple)):
        dataset = "+".join(sorted(str(item) for item in dataset))
    return f"{dataset}::{mechanism}"


def _window_bucket(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "OTHER"
    if value <= 10:
        return "SHORT"
    if value <= 60:
        return "MEDIUM"
    return "LONG"


def structural_fingerprint(expression, fields=None):
    """Bounded AST-like fingerprint with meaningful window buckets."""
    known = {str(item).lower() for item in (fields or [])}
    output = []
    for token in _TOKEN_RE.findall(str(expression or "").lower()):
        if token in known:
            output.append("<field>")
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            output.append(f"<number:{_window_bucket(token)}>")
        elif re.fullmatch(r"[a-z_][a-z0-9_]*", token):
            output.append(token if token in _OPERATORS else "<identifier>")
        else:
            output.append(token)
    return " ".join(output)


def token_ngram_fingerprints(expression, fields=None):
    """Return token n-grams; this is not claimed to be a parsed AST subtree."""
    tokens = structural_fingerprint(expression, fields).split()
    return frozenset(
        " ".join(tokens[index:index + width])
        for width in (2, 3, 4, 5)
        for index in range(max(0, len(tokens) - width + 1))
    )


def subtree_fingerprints(expression, fields=None):
    """Compatibility alias; prefer :func:`token_ngram_fingerprints`."""
    return token_ngram_fingerprints(expression, fields)


def syntax_diversity(record, pool):
    current = structural_fingerprint(_get(record, "expression", ""), _field_ids(record))
    other = [structural_fingerprint(_get(item, "expression", ""), _field_ids(item)) for item in pool]
    if not other:
        return {"status": "NO_POOL", "nearest": None, "score": 1.0}
    same = current in other
    return {"status": "AVAILABLE", "nearest": 1.0 if same else 0.0, "score": 0.0 if same else 1.0}


def _series_points(record):
    for key in ("returns", "pnl", "pnl_series", "signal_returns"):
        values = _get(record, key)
        if not isinstance(values, (list, tuple)):
            continue
        points = []
        for index, item in enumerate(values):
            date = None
            if isinstance(item, dict):
                date = item.get("date") or item.get("timestamp") or item.get("time")
                item = item.get("return", item.get("returns", item.get("pnl", item.get("value"))))
            try:
                value = float(item)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points.append((str(date) if date is not None else index, value))
        if len(points) >= 3:
            return points
    return None


def _corr_evidence(left, right):
    if not left or not right:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "missing series"}, status="UNAVAILABLE")
    left, right = dict(left), dict(right)
    keys = sorted(set(left) & set(right))
    overlap_ratio = len(keys) / max(len(left), len(right))
    if len(keys) < 3:
        return annotate_evidence({"status": "UNAVAILABLE",
                                  "reason": "fewer than 3 date-aligned observations", "overlap_count": len(keys),
                                  "overlap_ratio": overlap_ratio}, status="UNAVAILABLE")
    a, b = [left[key] for key in keys], [right[key] for key in keys]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    da = sum((value - ma) ** 2 for value in a)
    db = sum((value - mb) ** 2 for value in b)
    if da <= 0 or db <= 0:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "constant series",
                                  "overlap_count": len(keys), "overlap_ratio": overlap_ratio}, status="UNAVAILABLE")
    signed = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(da * db)
    return annotate_evidence({"status": "AVAILABLE", "signed_corr": signed,
                              "abs_corr": abs(signed), "overlap_count": len(keys),
                              "overlap_ratio": overlap_ratio},
                             status="INCONCLUSIVE", availability="AVAILABLE",
                             quality="VERIFIED", decision="INCONCLUSIVE")


def empirical_pool_summary(record, pool):
    rows = [_corr_evidence(_series_points(record), _series_points(item)) for item in pool]
    correlations = [row["signed_corr"] for row in rows if row.get("signed_corr") is not None]
    if not correlations:
        return annotate_evidence({"status": "UNAVAILABLE", "max_corr": None,
                                  "median_corr": None, "nearest_corr": None, "incremental_value": None,
                                  "correlations": rows}, status="UNAVAILABLE")
    ordered = sorted(correlations)
    max_corr = max(correlations, key=abs)
    incremental = max(0.0, 1.0 - max(abs(value) for value in correlations))
    return annotate_evidence({"status": "AVAILABLE", "max_corr": max_corr,
                               "median_corr": ordered[len(ordered) // 2], "nearest_corr": max_corr,
                               "incremental_value": incremental, "correlations": rows},
                              status="INCONCLUSIVE", availability="AVAILABLE",
                              quality="VERIFIED", decision="INCONCLUSIVE")


def incremental_novelty(record, pool):
    syntax = syntax_diversity(record, pool)
    empirical = empirical_pool_summary(record, pool)
    fields = set(_field_ids(record))
    overlap = max((len(fields & set(_field_ids(item))) / len(fields | set(_field_ids(item)))
                   for item in pool if fields or _field_ids(item)), default=0.0)
    empirical_score = empirical["incremental_value"]
    score = (0.4 * syntax["score"] + 0.2 * (1.0 - overlap) + 0.4 * empirical_score
             if empirical_score is not None else 0.7 * syntax["score"] + 0.3 * (1.0 - overlap))
    return {"score": round(max(0.0, min(1.0, score)), 6), "syntax": syntax,
            "empirical": empirical, "field_overlap": round(overlap, 6)}


def pareto_front(records):
    dimensions = ("sharpe", "fitness", "turnover", "margin", "drawdown", "novelty")
    maximize = {"sharpe", "fitness", "margin", "novelty"}

    def numeric(row, key):
        raw = row.get(key)
        if raw is None and key == "novelty":
            raw = row.get("novelty_score", 0.0)
        try:
            raw = float(raw)
            return raw if math.isfinite(raw) else None
        except (TypeError, ValueError):
            return None

    valid = []
    for record in records or []:
        row = dict(record) if isinstance(record, dict) else {key: _get(record, key) for key in dimensions}
        metrics = _get(record, "metrics", {}) or {}
        for key in dimensions:
            row.setdefault(key, metrics.get(key))
        if all(numeric(row, key) is not None for key in dimensions):
            valid.append((record, row))
    result = []
    for candidate, row in valid:
        dominated = False
        for other, other_row in valid:
            if other is candidate:
                continue
            comparisons = [(numeric(other_row, key), numeric(row, key), key in maximize) for key in dimensions]
            no_worse = all(left >= right if high else left <= right for left, right, high in comparisons)
            better = any(left > right if high else left < right for left, right, high in comparisons)
            if no_worse and better:
                dominated = True
                break
        if not dominated:
            result.append(candidate)
    return result


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
