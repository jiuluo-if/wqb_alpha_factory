"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Represent auditable experiments and append-only recovery evidence.
READ WHEN: changing experiment serialization, trajectory, or checkpoints.
DO NOT USE FOR: treating derived memory as immutable platform truth.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields

from .expression import canonical_expression
from .schema import CREATED_BY_VERSION, TRAJECTORY_VERSION

ACTIVE_EXECUTION_STATUSES = frozenset({"PENDING", "RUNNING", "SUBMITTING"})
UNKNOWN_STATUSES = frozenset({"SUBMIT_UNKNOWN", "UNKNOWN"})
UNRESOLVED_STATUSES = ACTIVE_EXECUTION_STATUSES | UNKNOWN_STATUSES
# ``SUBMIT_UNKNOWN`` is intentionally excluded: it requires read-only
# reconciliation, while ordinary UNKNOWN jobs may still be polled/recovered.
RECOVERABLE_STATUSES = ACTIVE_EXECUTION_STATUSES | frozenset({"UNKNOWN"})
TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "SKIPPED", "SKIPPED_STALE", "SKIPPED_UNKNOWN"})

# Append-only trajectory revision marker.  The canonical first append keeps
# execution identity; a later ``RESEARCH_SETTLED`` row re-persists the settled
# research evidence for the same Experiment.  It is not a second execution.
TRAJECTORY_REVISION_KEY = "trajectory_revision"
RESEARCH_SETTLED_REVISION = "RESEARCH_SETTLED"

# Execution identity is immutable: a settlement revision may change the later
# research evidence but never what was actually executed.
IDENTITY_FIELDS = (
    "id", "round", "hypothesis_id", "expression", "settings", "fields_used",
    "datasets", "candidate_id", "proposal_id", "submission_fingerprint",
    "submission_started_at", "parent_expression", "lineage_id", "created_at",
)

# One Experiment occupies a bounded number of rows in the append-only file
# (canonical first append plus settlement revisions), so a restart only needs
# to read enough tail lines to reconstruct the recent in-memory window.
_LOAD_LINES_PER_EXPERIMENT = 4


def same_execution_identity(left, right):
    """True when two trajectory rows describe the same executed Experiment."""
    return {key: left.get(key) for key in IDENTITY_FIELDS} == {
        key: right.get(key) for key in IDENTITY_FIELDS
    }


def json_literal_prefilter(value):
    """Return a safe raw-substring prefilter for a JSONL line, else ``None``.

    A JSON string escapes quote, backslash and control characters, and an
    ``ensure_ascii=True`` writer escapes non-ASCII characters, so those values
    cannot be prefiltered by a raw substring test without risking a false
    negative.  ``None`` means "decode every line" (fail-closed).
    """
    if not value:
        return None
    if any(ch in '"\\' or ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        return None
    return value


def literal_line_matcher(literals):
    """Return a raw-line matcher for already safe literal tokens.

    One token stays a plain ``str`` (``in`` is already the fastest check),
    while a batch becomes a single compiled alternation so a 32-expression
    read costs one C-level scan per line instead of 32 Python substring
    tests.  Callers must only pass tokens produced by
    :func:`json_literal_prefilter`.
    """
    if isinstance(literals, str):
        return literals
    tokens = tuple(sorted(set(literals or ())))
    if not tokens:
        return None
    if len(tokens) == 1:
        return tokens[0]
    return re.compile("|".join(re.escape(token) for token in tokens))


def _line_matches(matcher, line):
    if isinstance(matcher, str):
        return matcher in line
    return matcher.search(line) is not None


def merge_canonical_candidate(references, latest, key, row):
    """Keep the first legal identity reference and the latest legal revision."""
    reference = references.get(key)
    if reference is not None and not same_execution_identity(reference, row):
        return
    references.setdefault(key, row)
    latest[key] = row


def dataset_ref(value):
    """数据集条目归一化为字符串 id。

    proposal 的 datasets 允许 ``{"id": "pv1", "name": ...}`` 字典形态，
    但 trajectory 去重、ResearchState 聚合等消费方要求数据集是可哈希
    字符串；在 Experiment 入口单点归一化（dict 取 id，缺 id 退化 name）。
    """
    if isinstance(value, dict):
        value = value.get("id") or value.get("name")
    return str(value) if value is not None else None


@dataclass
class Experiment:
    round: int
    hypothesis_id: str
    expression: str
    settings: dict
    fields_used: list
    datasets: list | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    candidate_id: object = None
    proposal_id: object = None
    submission_fingerprint: object = None
    submission_started_at: object = None
    field_source: object = None
    field_understanding: object = None
    field_analysis: object = None
    field_hypothesis_basis: object = None
    operator_evidence: object = None
    template_id: object = None
    template_family: object = None
    template_stage_path: object = None
    template_ref: object = None
    template_slots: object = None
    search_evidence: object = None
    search_outcome: object = None
    provisional_outcome: object = None
    final_outcome: object = None
    robustness_evidence: object = None
    incremental_evidence: object = None
    pnl_evidence: object = None
    research_classification: object = None
    research_evidence_bundle: object = None
    submission_eligibility: object = None
    novelty_score: object = None
    allocation_arm: object = None
    allocation_key: object = None
    factory_session_id: object = None
    proposal_origin: object = None
    research_layer: object = None
    self_correlation: object = None
    status: str = "PENDING"
    metrics: object = None
    error: object = None
    alpha_id: object = None
    progress_url: object = None
    skip_record: object = None
    mutation: object = None
    lineage_id: object = None
    experiment_stage: object = None
    research_role: object = None
    change_type: object = None
    parent_expression: object = None
    child_economic_hypothesis: object = None
    changed_variable: object = None
    expected_failure_modes: list = field(default_factory=list)
    tuning_risk: object = None
    rationale: object = None
    direction: object = None
    economic_mechanism: object = None
    direction_transform: object = None
    self_correlation_impact: object = None
    expected_horizon: object = None
    falsification: object = None
    health: object = None
    yearly_evidence: object = None
    validation_plan: object = None
    validation_report: object = None
    validation_status: object = None
    elapsed_sec: object = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.settings = dict(self.settings)
        self.fields_used = list(self.fields_used)
        self.datasets = [
            ref for ref in (dataset_ref(item) for item in (self.datasets or []))
            if ref
        ]
        self.expected_failure_modes = list(self.expected_failure_modes or [])

    def to_dict(self):
        data = {
            "schema_version": TRAJECTORY_VERSION,
            "created_by_version": CREATED_BY_VERSION,
        }
        data.update({item.name: getattr(self, item.name) for item in dataclass_fields(self)})
        return data

    @classmethod
    def from_dict(cls, data):
        exp = cls(
            data["round"], data["hypothesis_id"], data["expression"],
            data["settings"], data["fields_used"], data.get("datasets"),
        )
        field_names = {item.name for item in dataclass_fields(cls)}
        for name in field_names:
            if name in {"round", "hypothesis_id", "expression", "settings", "fields_used", "datasets"}:
                continue
            if name in data:
                setattr(exp, name, data[name])
        exp.id = data["id"]
        exp.status = data["status"]
        exp.expected_failure_modes = data.get("expected_failure_modes") or []
        exp.created_at = data.get("created_at", 0)
        return exp


class Trajectory:
    """Append-only JSONL history + in-memory recent window.

    The full history is always preserved on disk (trajectory.jsonl);
    memory keeps only the last `max_len` experiments for iteration context.
    """

    COMPLETED_EXPRESSION_CACHE_MAX = 512

    def __init__(self, max_len=100, path=None, persist=True):
        self.experiments = []
        try:
            self.max_len = max(1, int(max_len))
        except (TypeError, ValueError):
            self.max_len = 100
        self.path = path
        self.persist = bool(persist)
        self._recent_ids = set()
        self._completed_expression_cache = {}
        self._append_batch_scope = None
        self._append_batch_known = None

    def add(self, experiment):
        """Append one experiment with cross-restart exactly-once protection."""
        self.add_many([experiment])

    def add_many(self, experiments):
        """Append a batch after one streaming ID reconciliation.

        ``_recent_ids`` is intentionally bounded with the in-memory window,
        so it cannot prove that an old experiment was not already appended
        before a restart.  Reconcile the small incoming batch against the
        append-only file once, then append only unseen rows.  No persistent
        ID sidecar or unbounded in-memory index is created.
        """
        experiments = list(experiments or [])
        if not experiments:
            return []
        candidate_ids = {experiment.id for experiment in experiments}
        known_ids = candidate_ids & self._recent_ids
        scoped_ids = set()
        if self._append_batch_scope is not None:
            scoped_ids = candidate_ids & self._append_batch_scope
            known_ids.update(scoped_ids & (self._append_batch_known or set()))
        if self.path:
            uncached_ids = candidate_ids - known_ids - scoped_ids
            if uncached_ids:
                known_ids.update(self.contains_ids(uncached_ids))
        added = []
        to_persist = []
        for experiment in experiments:
            if experiment.id in known_ids:
                continue
            self.experiments.append(experiment)
            self._recent_ids.add(experiment.id)
            known_ids.add(experiment.id)
            if experiment.id in scoped_ids:
                self._append_batch_known.add(experiment.id)
            self._completed_expression_cache.clear()
            if self.path and self.persist:
                to_persist.append(experiment)
            added.append(experiment)
        if self.path and self.persist and to_persist:
            self._append_jsonl_many(to_persist)
        if len(self.experiments) > self.max_len:
            self.experiments = self.experiments[-self.max_len:]
            self._recent_ids = {e.id for e in self.experiments}
        return added

    def begin_append_batch(self, experiments):
        """Reconcile one incoming simulation batch against history once."""
        scope = {
            experiment.id for experiment in (experiments or [])
            if getattr(experiment, "id", None)
        }
        known = scope & self._recent_ids
        if self.path and scope - known:
            known.update(self.contains_ids(scope - known))
        self._append_batch_scope = scope
        self._append_batch_known = known

    def end_append_batch(self):
        """Release transient batch identities after callbacks finish."""
        self._append_batch_scope = None
        self._append_batch_known = None

    def settle(self, experiment):
        """Append one legal settlement revision for an appended Experiment.

        Returns ``False`` when the identical revision is already persisted.
        """
        return bool(self.settle_many([experiment]))

    def settle_many(self, experiments):
        """Persist later-settled research evidence as reviewable revisions.

        The canonical first append keeps execution identity; this only
        re-persists later-aggregated evidence (validation report, incremental
        evidence, final outcome, research classification) under the same
        ``id``.  It never creates a second Simulation, a second store or a new
        execution identity.  A missing canonical row or an attempted identity
        change is refused (fail-closed) instead of silently overwriting
        already-executed facts.
        """
        if not self.path or not self.persist:
            return []
        pending = [
            item for item in (experiments or ()) if getattr(item, "id", None)
        ]
        if not pending:
            return []
        candidate_ids = {item.id for item in pending}
        references = {}
        latest = {}
        for row in self.iter_rows() or ():
            row_id = row.get("id")
            if row_id not in candidate_ids:
                continue
            if row_id not in references:
                references[row_id] = row
            latest[row_id] = row
        settled = []
        for experiment in pending:
            reference = references.get(experiment.id)
            if reference is None:
                raise ValueError(
                    "settlement revision requires a persisted Experiment: "
                    f"{experiment.id}"
                )
            row = experiment.to_dict()
            if not same_execution_identity(reference, row):
                raise ValueError(
                    "settlement revision cannot change execution identity: "
                    f"{experiment.id}"
                )
            row[TRAJECTORY_REVISION_KEY] = RESEARCH_SETTLED_REVISION
            if latest.get(experiment.id) == row:
                continue
            settled.append(experiment)
        self._append_jsonl_many(settled, revision=RESEARCH_SETTLED_REVISION)
        return settled

    def _append_jsonl(self, experiment):
        self._append_jsonl_many([experiment])

    def _append_jsonl_many(self, experiments, revision=None):
        """Append a batch with one flush/fsync while preserving JSONL order."""
        if not experiments:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            for experiment in experiments:
                row = experiment.to_dict()
                if revision:
                    row[TRAJECTORY_REVISION_KEY] = revision
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def contains_id(self, experiment_id):
        """Check an append-only trajectory id without materializing history."""
        return experiment_id in self.contains_ids({experiment_id})

    def contains_ids(self, experiment_ids):
        """Find a set of IDs in one streaming pass over trajectory."""
        if not self.persist:
            return set()
        if isinstance(experiment_ids, (str, int)):
            experiment_ids = {experiment_ids}
        targets = {value for value in (experiment_ids or set()) if value}
        if not targets or not self.path or not os.path.exists(self.path):
            return set()
        literals = {target: json_literal_prefilter(str(target)) for target in targets}
        prefilter = (
            tuple(sorted(set(literals.values())))
            if all(literal is not None for literal in literals.values())
            else None
        )
        found = set()
        for row in self.iter_rows(prefilter=prefilter) or ():
            row_id = row.get("id")
            if row_id in targets:
                found.add(row_id)
                if found == targets:
                    break
        return found

    def iter_ids(self):
        """Stream trajectory ids without constructing Experiment objects."""
        for row in self.iter_rows() or ():
            if row.get("id"):
                yield row["id"]

    def iter_rows(self, *, stats=None, prefilter=None):
        """Stream valid raw trajectory objects without retaining history.

        This is a read-only primitive for bounded identity/audit passes.  It
        deliberately yields dictionaries rather than ``Experiment`` objects
        so callers do not materialize the day-long append-only file or create
        a persistent sidecar index.

        ``prefilter`` is an optional literal (or tuple of literals) that every
        requested row must contain; it only skips JSON decoding of lines that
        cannot match and never changes which rows are yielded.  A line that
        carries a JSON escape is always decoded, because a decoded value can
        differ from its raw text.
        """
        if not self.persist:
            return
        if not self.path or not os.path.exists(self.path):
            return
        matcher = literal_line_matcher(prefilter)
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    if matcher and "\\" not in line and not _line_matches(matcher, line):
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        if isinstance(stats, dict):
                            stats["invalid_rows"] = stats.get("invalid_rows", 0) + 1
                        continue
                    if isinstance(row, dict):
                        yield row
                    elif isinstance(stats, dict):
                        stats["invalid_rows"] = stats.get("invalid_rows", 0) + 1
        except OSError:
            return

    def find_completed_expression(self, expression):
        """Find a completed experiment by streaming the append-only history.

        The in-memory trajectory is intentionally bounded. Older parents must
        still be available for proposal validation without loading the whole
        day-long JSONL file into memory.
        """
        if not self.path or not expression or not os.path.exists(self.path):
            return None
        target = canonical_expression(expression)
        return self.find_completed_expressions([target]).get(target)

    def find_row(self, experiment_id):
        """Return the latest valid canonical row for one experiment identity.

        Bounded streaming pass that keeps only the most recent legal revision
        for the requested ``id``/``proposal_id``; used by agent-facing reads so
        they never surface an early DONE snapshot over later settled evidence.
        """
        if not self.persist or not self.path or not os.path.exists(self.path):
            return None
        target = str(experiment_id)
        reference = None
        latest = None
        for row in self.iter_rows(prefilter=json_literal_prefilter(target)) or ():
            if target not in (str(row.get("id")), str(row.get("proposal_id"))):
                continue
            if reference is not None and not same_execution_identity(reference, row):
                continue
            if reference is None:
                reference = row
            latest = row
        return latest

    def find_rows(self, experiment_ids):
        """Resolve several experiment identities with one streaming pass.

        ``find_row`` streams the append-only file per identity, so comparing a
        handful of ids costs one full scan each.  This owner-local batch
        primitive performs a single canonical merge pass for the whole set
        while keeping ``find_row``'s per-identity semantics: the latest legal
        revision wins and a row whose execution identity contradicts the first
        legal match is ignored.  Missing identities are reported as ``None``
        rather than fabricated.
        """
        if not self.persist or not self.path or not os.path.exists(self.path):
            return {}
        targets = {
            str(value) for value in (experiment_ids or ()) if value is not None
        }
        if not targets:
            return {}
        literals = {target: json_literal_prefilter(target) for target in targets}
        prefilter = (
            tuple(sorted(set(literals.values())))
            if all(literal is not None for literal in literals.values())
            else None
        )
        references = {}
        latest = {}
        for row in self.iter_rows(prefilter=prefilter) or ():
            row_id = str(row.get("id"))
            if row_id in targets:
                merge_canonical_candidate(references, latest, row_id, row)
            proposal_id = str(row.get("proposal_id"))
            if proposal_id in targets and proposal_id != row_id:
                merge_canonical_candidate(references, latest, proposal_id, row)
        return {target: latest.get(target) for target in targets}

    def iter_canonical_rows(self, *, since=None, until=None):
        """Stream the latest valid canonical row per experiment identity.

        ``iter_rows`` intentionally yields the append-only revisions (an early
        DONE row plus later ``RESEARCH_SETTLED`` revisions), so a raw consumer
        can see one Experiment twice.  This bounded streaming merge keeps only
        the most recent legal revision per identity and is the single place
        agent-facing reads resolve "one experiment -> one canonical record".
        Optional ``since``/``until`` epoch bounds filter on ``created_at``
        *before* merging, so a bounded time window still reaches rows that the
        in-memory ``max_len`` window no longer holds.
        """
        if not self.persist or not self.path or not os.path.exists(self.path):
            return
        merged = {}
        for row in self.iter_rows() or ():
            row_id = row.get("id")
            if not row_id:
                continue
            if since is not None or until is not None:
                created_at = row.get("created_at")
                if not isinstance(created_at, (int, float)):
                    continue
                if since is not None and created_at < since:
                    continue
                if until is not None and created_at >= until:
                    continue
            reference = merged.get(row_id)
            if reference is not None and not same_execution_identity(reference, row):
                continue
            merged[row_id] = row
        for row in merged.values():
            yield row

    def find_completed_expressions(self, expressions):
        """Resolve several old parents with one streaming history pass.

        Proposal batches commonly validate multiple CHILD/ROBUSTNESS entries.
        Reading the append-only trajectory once per parent makes that gate
        scale with the batch, while the bounded cache retains repeat-call
        idempotency without creating a new sidecar index.
        """
        if not self.persist:
            return {}
        targets = {
            canonical_expression(expression)
            for expression in (expressions or [])
            if isinstance(expression, str) and expression.strip()
        }
        if not targets or not self.path or not os.path.exists(self.path):
            return {}
        pending = targets - self._completed_expression_cache.keys()
        found = {}
        identities = {}
        if pending:
            try:
                with open(self.path, encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                continue
                            target = canonical_expression(row.get("expression", ""))
                            if target not in pending:
                                continue
                            if row.get("status") != "DONE" or not row.get("metrics"):
                                continue
                            row_id = row.get("id")
                            reference = identities.get(row_id)
                            if reference is not None and not same_execution_identity(
                                reference, row
                            ):
                                continue
                            identities[row_id] = row
                            found[target] = Experiment.from_dict(row)
                        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                            continue
            except OSError:
                pass
            self._completed_expression_cache.update(
                {target: found.get(target) for target in pending}
            )
            overflow = (
                len(self._completed_expression_cache)
                - self.COMPLETED_EXPRESSION_CACHE_MAX
            )
            if overflow > 0:
                for target in list(self._completed_expression_cache)[:overflow]:
                    self._completed_expression_cache.pop(target, None)
        return {
            target: self._completed_expression_cache.get(target)
            for target in targets
        }

    def load(self):
        """Load the durable trajectory and merge its append-only revisions.

        One Experiment can occupy several rows: the canonical first append plus
        later settlement revisions (``RESEARCH_SETTLED``).  The owner merges
        them into the canonical current view so ``experiments`` /
        ``find_completed_expression`` never surface an early DONE snapshot over
        later settled evidence.  Only valid rows count, and a revision whose
        execution identity contradicts the first append is ignored, so the last
        valid evidence survives a corrupt tail.
        """
        if not self.persist:
            return self
        if self.path and os.path.exists(self.path):
            merged = self._merge_rows(self._tail_lines(
                self.max_len * _LOAD_LINES_PER_EXPERIMENT
            ))
            self.experiments = merged[-self.max_len:]
            self._recent_ids = {e.id for e in self.experiments}
            self._completed_expression_cache.clear()
        return self

    def _merge_rows(self, lines):
        """Merge append-only rows into the canonical current Experiment view."""
        merged = {}
        for line in lines or ():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict) or not row.get("id"):
                continue
            try:
                experiment = Experiment.from_dict(row)
            except (ValueError, KeyError, TypeError):
                continue
            previous = merged.get(experiment.id)
            if previous is not None and not same_execution_identity(
                previous.to_dict(), experiment.to_dict()
            ):
                continue
            merged.pop(experiment.id, None)
            merged[experiment.id] = experiment
        return list(merged.values())

    def _tail_lines(self, n):
        """Read the last up-to-``n`` lines under a hard byte ceiling.

        Normal trajectory rows are small, but a malformed or unexpectedly
        large row must not make an all-day restart allocate the whole history.
        Returning fewer rows is safer than unbounded memory growth; the raw
        append-only file remains untouched for later reconciliation.
        """
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                chunks = []
                newline_count = 0
                bytes_read = 0
                max_bytes = 64 * 1024 * 1024
                while pos > 0 and newline_count <= n and bytes_read < max_bytes:
                    size = min(1024 * 1024, pos)
                    size = min(size, max_bytes - bytes_read)
                    pos -= size
                    f.seek(pos)
                    chunk = f.read(size)
                    chunks.append(chunk)
                    bytes_read += len(chunk)
                    newline_count += chunk.count(b"\n")
        except OSError:
            return []
        data = b"".join(reversed(chunks))
        decoded = [ln.decode("utf-8", errors="replace") for ln in data.splitlines()[-n:]]
        return [ln for ln in decoded if ln.strip()]

    def recent(self, n=20):
        return self.experiments[-n:]

    def completed(self):
        return [e for e in self.experiments if e.metrics is not None]

    def expressions(self):
        return {e.expression for e in self.experiments}

    def to_dict(self):
        return {"experiments": [e.to_dict() for e in self.experiments]}

    @classmethod
    def from_dict(cls, data, max_len=100):
        traj = cls(max_len=max_len)
        traj.experiments = [Experiment.from_dict(e) for e in data.get("experiments", [])]
        traj.experiments = traj.experiments[-traj.max_len:]
        traj._recent_ids = {e.id for e in traj.experiments}
        traj._completed_expression_cache.clear()
        return traj


class ResearchState:
    def __init__(self, round_no=0, hypothesis=None, dataset=None, fields_used=None):
        self.round_no = round_no
        self.hypothesis = hypothesis
        self.dataset = dataset
        self.fields_used = fields_used or []
        self.started_at = time.time()

    def to_dict(self):
        return {
            "round_no": self.round_no,
            "hypothesis": self.hypothesis,
            "dataset": self.dataset,
            "fields_used": self.fields_used,
            "started_at": self.started_at,
        }
