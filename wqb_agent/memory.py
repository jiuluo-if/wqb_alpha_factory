"""ROLE: INTERNAL
AGENT_RELEVANCE: MEDIUM
PURPOSE: Maintain bounded derived workspace and decision memory.
READ WHEN: debugging workspace compression or memory persistence.
DO NOT USE FOR: replacing trajectory evidence or deciding hypotheses.

Experience Memory — three-tier compressed research memory.

Tiers
-----
- short_term : recent working memory (round recaps, pending reconciliations,
  low-confidence observations). Entries expire after short_term_window
  rounds; repeatedly-hit observations are promoted to long-term lessons,
  everything else moves to the garbage tier.
- long_term  : validated, evidence-backed memory (lessons / avoid / next /
  active_hypotheses / current_best). Only real simulation results may write
  here; every entry keeps source_round / evidence / confidence for audit.
- garbage    : soft-deleted entries (tombstones). Forgotten / superseded /
  expired entries move here with a reason and can be restored; they are
  physically purged only after garbage_max_age_rounds rounds (by the
  maintenance script, dry-run first).

Long history lives in trajectory.jsonl (append-only). experience.json keeps
the compressed long-term view; garbage.json keeps the tombstone log in a
separate file so the main memory file never bloats.
"""

import json
import math
import os
import re
import time
import uuid

from .artifacts import atomic_write_json_if_changed
from .expression import canonical_expression
from .memory_codec import (
    dict_list,
    pack_expressions,
    unpack_expressions,
)
from .memory_policy import expiration_partition, promotion_allowed, similar
from .memory_projection import (
    garbage_stats as project_garbage_stats,
)
from .memory_projection import (
    learning_context,
)
from .memory_projection import (
    next_with_fields as project_next_with_fields,
)
from .memory_projection import (
    recent_short_term as project_recent_short_term,
)
from .memory_projection import (
    top_next as project_top_next,
)
from .memory_replay import MemorySourceReplayConflict as _MemorySourceReplayConflict
from .memory_replay import find_source_entry, replay_source, source_receipt
from .schema import MEMORY_VERSION

MemorySourceReplayConflict = _MemorySourceReplayConflict

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# Hypothesis ids generated solely from a round are reconstructable from the
# trajectory and must not consume long-term memory forever. The latter
# alternatives cover pre-current-era ids already present in older state files.
_EPHEMERAL_HYPOTHESIS_ID = re.compile(
    r"^(?:h-(?:next|iter|llm|factory|space)-r\d+|"
    r"h-rb[0-9a-z]+-.+|rb[0-9a-z]+-hypothesis)$",
    re.IGNORECASE,
)

# Kinds allowed in the short-term tier.
SHORT_KINDS = ("recap", "pending", "observation")
_SOURCE_RECEIPT_LIMIT = 64


# Reasons recorded when an entry is soft-deleted into the garbage tier.
GARBAGE_REASONS = ("stale", "superseded", "deduped", "expired", "low_value",
                   "not_promoted", "user_removed")


class ExperienceMemory:
    def __init__(
        self,
        state_dir=".wqb_state",
        max_lessons=20,
        max_avoid=30,
        max_next=15,
        max_hypotheses=12,
        max_short_term=30,
        short_term_window=5,
        promote_hits=2,
        max_garbage=200,
        garbage_max_age_rounds=60,
        next_max_age_rounds=20,
        max_lineages=256,
        max_seen_expressions=4096,
        max_used_hypotheses=256,
        persist=True,
    ):
        self.state_dir = state_dir
        self.persist = bool(persist)
        self.max_lessons = self._cap(max_lessons, 20)
        self.max_avoid = self._cap(max_avoid, 30)
        self.max_next = self._cap(max_next, 15)
        self.max_hypotheses = self._cap(max_hypotheses, 12)
        self.max_short_term = self._cap(max_short_term, 30)
        self.short_term_window = self._cap(short_term_window, 5)
        self.promote_hits = self._cap(promote_hits, 2)
        self.max_garbage = self._cap(max_garbage, 200)
        self.garbage_max_age_rounds = self._cap(garbage_max_age_rounds, 60)
        self.next_max_age_rounds = self._cap(next_max_age_rounds, 20)
        self.max_lineages = max(1, self._cap(max_lineages, 256))
        try:
            self.max_seen_expressions = max(1, int(max_seen_expressions))
        except (TypeError, ValueError):
            self.max_seen_expressions = 4096
        try:
            self.max_used_hypotheses = max(1, int(max_used_hypotheses))
        except (TypeError, ValueError):
            self.max_used_hypotheses = 256
        self.current_best = None
        self.lessons = []
        self.avoid = []
        self.next = []
        self.active_hypotheses = []
        self.seen_expressions = set()
        self.used_hypotheses = set()
        self.updated_round = 0
        self.best_exhausted = False
        self.short_term = []
        # Bounded experiment-budget state; this is not a research lesson.
        self.lineages = {}
        self.garbage = []
        self._ensure_dir()

    # ------------------------------------------------------------------ I/O

    def _ensure_dir(self):
        if self.state_dir and self.persist:
            os.makedirs(self.state_dir, exist_ok=True)

    @staticmethod
    def _cap(value, fallback):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _number(value, default=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    def memory_path(self):
        return os.path.join(self.state_dir, "experience.json") if self.state_dir else None

    def garbage_path(self):
        return os.path.join(self.state_dir, "garbage.json") if self.state_dir else None

    def load(self):
        if not self.persist:
            return self
        path = self.memory_path()
        if not path or not os.path.exists(path):
            return self
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise TypeError("experience root must be an object")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # 核心记忆损坏：只保留一个稳定诊断备份后降级为空记忆继续
            # 启动，绝不崩溃——trajectory.jsonl 仍是完整证据源，可据此
            # 重建。固定备份名避免全天运行每次重启都生成新的冗余文件。
            backup = f"{path}.corrupt"
            try:
                if not os.path.exists(backup):
                    os.replace(path, backup)
            except OSError:
                pass
            self.current_best = None
            self.lessons = []
            self.avoid = []
            self.next = []
            self.active_hypotheses = []
            self.seen_expressions = set()
            self.used_hypotheses = set()
            self.best_exhausted = False
            self.updated_round = 0
            self.short_term = []
            self.lineages = {}
            self._load_garbage()
            print(
                f"[MEMORY] experience.json 损坏（{type(exc).__name__}），"
                f"已备份为 {backup} 并降级为空记忆启动。"
            )
            return self
        self.current_best = (
            data.get("current_best")
            if isinstance(data.get("current_best"), dict) else None
        )
        self.lessons = dict_list(data.get("lessons"))
        self.avoid = dict_list(data.get("avoid"))
        self.next = dict_list(data.get("next"))
        self.active_hypotheses = dict_list(data.get("active_hypotheses"))
        packed = data.get("seen_expressions_blob")
        if isinstance(packed, str):
            self.seen_expressions = unpack_expressions(packed)
        else:
            # Backward compatibility with the pre-factory JSON list format.
            legacy_expressions = data.get("seen_expressions", [])
            if not isinstance(legacy_expressions, list):
                legacy_expressions = []
            self.seen_expressions = {
                canonical_expression(value)
                for value in legacy_expressions
                if isinstance(value, str) and value.strip()
            }
        self._trim_seen_expressions()
        used_hypotheses = data.get("used_hypotheses", [])
        if not isinstance(used_hypotheses, list):
            used_hypotheses = []
        self.used_hypotheses = {
            str(value) for value in used_hypotheses
            if isinstance(value, (str, int)) and value
            and not _EPHEMERAL_HYPOTHESIS_ID.fullmatch(str(value))
        }
        self._trim_used_hypotheses()
        self.best_exhausted = bool(data.get("best_exhausted", False))
        try:
            self.updated_round = int(data.get("updated_round", 0) or 0)
        except (TypeError, ValueError):
            self.updated_round = 0
        # short_term may be absent in files written by older versions.
        self.short_term = dict_list(data.get("short_term"))
        raw_lineages = data.get("lineages", {})
        self.lineages = (
            {str(key): value for key, value in raw_lineages.items()
             if isinstance(value, dict)}
            if isinstance(raw_lineages, dict) else {}
        )
        self._load_garbage()
        # Enforce all memory caps at the trust boundary.  This only changes
        # the in-memory decision view; persistence remains owned by save().
        self.compress()
        return self

    def _load_garbage(self):
        if not self.persist:
            self.garbage = []
            return
        path = self.garbage_path()
        if not path or not os.path.exists(path):
            self.garbage = []
            return
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
            self.garbage = dict_list(value)
        except (json.JSONDecodeError, OSError):
            # A corrupt tombstone log must never break the main memory.
            self.garbage = []

    def save(self):
        if not self.state_dir or not self.persist:
            return None
        self.compress()
        data = {
            "schema_version": MEMORY_VERSION,
            "current_best": self.current_best,
            "lessons": self.lessons,
            "avoid": self.avoid,
            "next": self.next,
            "active_hypotheses": self.active_hypotheses,
            # Full expressions remain available in memory at runtime for
            # exact dedupe, but the durable decision view stores them in one
            # compressed field.  This removes megabytes of JSON punctuation
            # without creating a second redundant memory file.
            "seen_expressions_blob": pack_expressions(self.seen_expressions),
            "seen_expressions_count": len(self.seen_expressions),
            "used_hypotheses": sorted(self.used_hypotheses),
            "best_exhausted": self.best_exhausted,
            "updated_round": self.updated_round,
            "short_term": self.short_term,
            "lineages": self.lineages,
        }
        path = self.memory_path()
        atomic_write_json_if_changed(path, data)
        self._save_garbage()

    def _save_garbage(self):
        path = self.garbage_path()
        atomic_write_json_if_changed(path, self.garbage)

    # ---------------------------------------------------------- short term

    def _source_entry(self, source_key):
        return find_source_entry(
            source_key,
            (
                ("short_term", self.short_term),
                ("lesson", self.lessons),
                ("avoid", self.avoid),
                ("next", self.next),
                ("hypothesis", self.active_hypotheses),
            ),
            self.garbage,
        )

    def _source_replay(self, source_key, kind, semantic):
        return replay_source(
            source_key,
            kind,
            semantic,
            (
                ("short_term", self.short_term),
                ("lesson", self.lessons),
                ("avoid", self.avoid),
                ("next", self.next),
                ("hypothesis", self.active_hypotheses),
            ),
            self.garbage,
        )

    def _stamp_source(self, entry, source_key, semantic):
        entry.update(
            source_receipt(entry, source_key, semantic, _SOURCE_RECEIPT_LIMIT)
        )
        return entry

    def add_short_term(self, kind, text, round_no, evidence=1, detail=None,
                       source_key=None):
        """Add a short-term entry. A similar existing entry is merged:
        hits + 1, text/round refreshed — repeated observations accumulate
        hits and become promotion candidates when they expire."""
        if kind not in SHORT_KINDS:
            raise ValueError(f"short-term kind must be one of {SHORT_KINDS}")
        semantic = {"kind": kind, "text": text, "round": round_no,
                    "evidence": evidence, "detail": detail}
        replay = self._source_replay(source_key, "short_term", semantic)
        if replay is not None:
            return replay
        lineage = detail.get("lineage") if isinstance(detail, dict) else None
        for entry in self.short_term:
            if entry.get("kind") == kind and similar(
                entry.get("text", ""), text, threshold=0.7
            ):
                entry["hits"] = entry.get("hits", 1) + 1
                entry["text"] = text
                entry["round"] = round_no
                entry["updated"] = time.time()
                if lineage:
                    entry["lineages"] = sorted(
                        set(entry.get("lineages") or []) | {lineage}
                    )
                self._stamp_source(entry, source_key, semantic)
                self._merge_learning_metadata(entry, detail)
                return entry
        entry = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "text": text,
            "round": round_no,
            "evidence": evidence,
            "hits": 1,
            "detail": detail,
            "lineages": [lineage] if lineage else [],
            "created": time.time(),
            "updated": time.time(),
        }
        self._stamp_source(entry, source_key, semantic)
        self._merge_learning_metadata(entry, detail)
        self.short_term.append(entry)
        return entry

    @staticmethod
    def _merge_learning_metadata(entry, detail):
        """Copy the small structured learning projection to its owner row."""
        if not isinstance(detail, dict):
            return
        for key in (
            "optimization_trial", "parent_id", "outcome", "mechanism",
            "changed_variable",
            "hypothesis_outcome", "mechanism_learning", "unresolved_question",
            "competing_explanations", "next_discriminating_question",
            "evidence_needed", "evidence_refs", "parent_hypothesis",
            "parent_expression", "change_reason", "lineages",
            "independent_lineages", "confirmation_status",
            "agent_interpretation", "outcome_reason",
        ):
            if key in detail:
                entry[key] = detail[key]

    def recent_short_term(self, n=5):
        return project_recent_short_term(self.short_term, n)

    def bump_short_term(self, entry_id):
        for entry in self.short_term:
            if entry.get("id") == entry_id:
                entry["hits"] = entry.get("hits", 1) + 1
                entry["updated"] = time.time()
                return entry
        return None

    def expire_short_term(self, now_round=None, save=False):
        """Expire short-term entries older than the window. Observation
        entries with enough hits are promoted to long-term lessons; the rest
        move to the garbage tier (soft delete). Returns (promoted, trashed)."""
        now_round = now_round if now_round is not None else self.updated_round
        promoted, trashed = [], []
        kept, expired = expiration_partition(
            self.short_term, now_round, self.short_term_window
        )
        for entry in expired:
            new_lesson = self._promote_short_term(entry, now_round)
            if new_lesson is not None:
                promoted.append(new_lesson)
            else:
                self.move_to_garbage(
                    "short_term", entry, reason="expired",
                    note=self._expire_note(entry), round_no=now_round,
                )
                trashed.append(entry)
        self.short_term = kept
        if save:
            self.save()
        return promoted, trashed

    def _promote_short_term(self, entry, now_round):
        """Observation entries that kept being repeated (hits >= threshold)
        become long-term lessons; other kinds never auto-promote."""
        # Repeating a result in the same lineage is not independent evidence.
        # Legacy entries with no lineage are deliberately retained as
        # observations rather than promoted on hit count alone.
        if not promotion_allowed(entry, self.promote_hits):
            return None
        evidence = entry.get("evidence", 1) * min(entry.get("hits", 1), 3)
        confidence = min(0.6, 0.2 + 0.1 * entry.get("hits", 0))
        lesson = self.add_lesson(
            entry["text"], now_round, evidence=evidence, confidence=confidence,
            source_key=(f"promotion:{entry['source_key']}"
                        if entry.get("source_key") else None),
            metadata={
                key: entry[key]
                for key in (
                    "hypothesis_outcome", "mechanism_learning", "unresolved_question",
                    "competing_explanations", "next_discriminating_question",
                    "evidence_needed", "evidence_refs", "parent_hypothesis",
                    "parent_expression", "change_reason", "lineages",
                    "independent_lineages", "confirmation_status",
                    "agent_interpretation", "outcome_reason",
                )
                if key in entry
            },
        )
        receipts = [
            receipt for receipt in entry.get("_source_receipts", [])
            if isinstance(receipt, dict) and receipt.get("source_key")
        ]
        if entry.get("source_key") and not any(
            receipt.get("source_key") == entry["source_key"]
            for receipt in receipts
        ):
            receipts.append({
                "source_key": entry["source_key"],
                "_source_semantic": entry.get("_source_semantic"),
            })
        if receipts:
            existing = [
                receipt for receipt in lesson.get("_source_receipts", [])
                if isinstance(receipt, dict)
            ]
            by_key = {
                receipt.get("source_key"): receipt
                for receipt in existing + receipts
                if receipt.get("source_key")
            }
            lesson["_source_receipts"] = list(by_key.values())[-_SOURCE_RECEIPT_LIMIT:]
        return lesson

    def _expire_note(self, entry):
        return (
            f"short-term [{entry.get('kind')}] not promoted "
            f"(hits={entry.get('hits', 0)} < {self.promote_hits} or non-promotable)"
        )

    def confirm_pending(self, entry_id, verdict, round_no, detail=None):
        """Reconcile a short-term 'pending' entry after read-only checks.
        verdict: 'success' | 'failed' | 'neutral'. Resolves the entry into
        long-term memory (or garbage) and removes it from short term."""
        for i, entry in enumerate(self.short_term):
            if entry.get("id") == entry_id:
                self.short_term.pop(i)
                text = detail or entry.get("text", "")
                if verdict == "success":
                    return self.add_lesson(
                        text, round_no, evidence=2, confidence=0.5
                    )
                if verdict == "failed":
                    self.add_avoid(
                        text[:80], f"confirmed after reconciliation: {text}",
                        round_no,
                    )
                    return self.add_lesson(
                        text, round_no, evidence=1, confidence=0.3
                    )
                # neutral: not a directional conclusion.
                self.move_to_garbage(
                    "short_term", entry, reason="not_promoted",
                    note="pending reconciliation was neutral", round_no=round_no,
                )
                return None
        return None

    # ---------------------------------------------------------------- garbage

    def move_to_garbage(self, kind, entry, reason, note="", round_no=None):
        """Soft-delete an entry: keep a tombstone instead of destroying it."""
        if reason not in GARBAGE_REASONS and not note:
            note = f"reason={reason}"
        tomb = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "entry": entry,
            "reason": reason,
            "note": note,
            "moved_round": round_no if round_no is not None else self.updated_round,
            "moved_at": time.time(),
        }
        self.garbage.append(tomb)
        if len(self.garbage) > self.max_garbage:
            self.purge_garbage(
                max_age_rounds=0, now_round=round_no, dry_run=False
            )
        return tomb

    def restore_from_garbage(self, tomb_id):
        """Restore a tombstoned entry back to its tier. Returns the restored
        entry or None if the id is unknown."""
        for i, tomb in enumerate(self.garbage):
            if tomb.get("id") == tomb_id:
                entry = tomb.get("entry")
                kind = tomb.get("kind")
                self.garbage.pop(i)
                if kind == "lesson":
                    self.lessons.append(entry)
                elif kind == "avoid":
                    self.avoid.append(entry)
                elif kind == "next":
                    self.next.append(entry)
                elif kind == "short_term":
                    self.short_term.append(entry)
                elif kind == "hypothesis":
                    self.active_hypotheses.append(entry)
                else:
                    return None
                return entry
        return None

    def purge_garbage(self, max_age_rounds=None, now_round=None, dry_run=True):
        """Physically delete tombstones older than max_age_rounds.
        Returns the list that would be / was deleted."""
        max_age_rounds = (
            self.garbage_max_age_rounds
            if max_age_rounds is None else max_age_rounds
        )
        now_round = now_round if now_round is not None else self.updated_round
        doomed = [
            t for t in self.garbage
            if (now_round - t.get("moved_round", now_round)) > max_age_rounds
        ]
        if not dry_run and doomed:
            doomed_ids = {t.get("id") for t in doomed}
            self.garbage = [t for t in self.garbage if t.get("id") not in doomed_ids]
            self._save_garbage()
        return doomed

    def garbage_stats(self):
        return project_garbage_stats(self.garbage)

    # ------------------------------------------------------------- lessons

    def add_lesson(self, claim, source_round, evidence, confidence=0.5,
                   metadata=None, source_key=None):
        semantic = {"claim": claim, "source_round": source_round,
                    "evidence": evidence, "confidence": confidence,
                    "metadata": metadata}
        replay = self._source_replay(source_key, "lesson", semantic)
        if replay is not None:
            return replay
        for lesson in self.lessons:
            if similar(lesson["claim"], claim, threshold=0.8):
                self._stamp_source(lesson, source_key, semantic)
                lesson["source_round"] = source_round
                lesson["evidence"] = lesson.get("evidence", 0) + evidence
                lesson["confidence"] = min(1.0, lesson.get("confidence", 0.5) + 0.15)
                lesson["last_used"] = time.time()
                lesson["updated"] = time.time()
                if isinstance(metadata, dict) and metadata:
                    lesson["metadata"] = dict(metadata)
                return lesson
        entry = {
            "id": uuid.uuid4().hex[:8],
            "claim": claim,
            "source_round": source_round,
            "evidence": evidence,
            "confidence": min(confidence, 1.0),
            "created": time.time(),
            "updated": time.time(),
            "last_used": time.time(),
        }
        self._stamp_source(entry, source_key, semantic)
        if isinstance(metadata, dict) and metadata:
            entry["metadata"] = dict(metadata)
        self.lessons.append(entry)
        return entry

    # --------------------------------------------------------------- avoid

    def add_avoid(self, direction, reason, source_round, source_key=None):
        semantic = {"direction": direction, "reason": reason,
                    "source_round": source_round}
        replay = self._source_replay(source_key, "avoid", semantic)
        if replay is not None:
            return replay
        for item in self.avoid:
            if item.get("direction") == direction:
                self._stamp_source(item, source_key, semantic)
                item["reason"] = reason
                item["source_round"] = source_round
                item["updated"] = time.time()
                return item
        entry = {
            "id": uuid.uuid4().hex[:8],
            "direction": direction,
            "reason": reason,
            "source_round": source_round,
            "created": time.time(),
            "updated": time.time(),
        }
        self._stamp_source(entry, source_key, semantic)
        self.avoid.append(entry)
        return entry

    def reconcile_avoid(self, direction, reason, source_round):
        """Correct one evidence-backed avoid record without raw JSON edits.

        Reflection stores expression keys truncated to 80 characters.  This
        helper also collapses an accidental full-expression duplicate created
        by a repair/migration caller, preserving the established key.
        """
        key = str(direction)[:80]
        matches = [
            item for item in self.avoid
            if item.get("direction") == key or item.get("direction") == direction
        ]
        if matches:
            target = next((item for item in matches if item.get("direction") == key), matches[0])
            target["direction"] = key
            target["reason"] = reason
            target["source_round"] = source_round
            target["updated"] = time.time()
            # 用对象身份（id）剔除匹配项：dict 值相等不代表同一条记录，
            # `not in matches` 的 == 语义可能误删内容恰好相同的另一条。
            matched_ids = {id(item) for item in matches}
            self.avoid = [
                item for item in self.avoid
                if item is target or id(item) not in matched_ids
            ]
            return target
        return self.add_avoid(key, reason, source_round)

    def is_avoided(self, direction):
        return any(item["direction"] == direction for item in self.avoid)

    # ---------------------------------------------------------------- next

    def add_next(self, idea, priority, source, round_no, fields=None, datasets=None,
                 metadata=None, source_key=None):
        """Register a next experiment idea. fields/datasets make the idea
        directly actionable: the next round fetches these fields first."""
        semantic = {"idea": idea, "priority": priority, "source": source,
                    "round_no": round_no, "fields": sorted(set(fields or [])),
                    "datasets": sorted(set(datasets or [])), "metadata": metadata}
        replay = self._source_replay(source_key, "next", semantic)
        if replay is not None:
            return replay
        for item in self.next:
            if item.get("idea") == idea:
                self._stamp_source(item, source_key, semantic)
                item["priority"] = max(item.get("priority", 0), priority)
                item["source"] = source
                if fields:
                    item["fields"] = sorted(set(item.get("fields") or []) | set(fields))
                if datasets:
                    item["datasets"] = sorted(
                        set(item.get("datasets") or []) | set(datasets)
                    )
                self._merge_next_metadata(item, metadata)
                return item
        entry = {
            "id": uuid.uuid4().hex[:8],
            "idea": idea,
            "priority": priority,
            "source": source,
            "round": round_no,
            "created": time.time(),
        }
        self._stamp_source(entry, source_key, semantic)
        if fields:
            entry["fields"] = sorted(set(fields))
        if datasets:
            entry["datasets"] = sorted(set(datasets))
        self._merge_next_metadata(entry, metadata)
        self.next.append(entry)
        return entry

    @staticmethod
    def _merge_next_metadata(entry, metadata):
        if not isinstance(metadata, dict):
            return
        for key in (
            "parent_hypothesis", "parent_expression", "change_reason",
            "unresolved_question", "competing_explanations",
            "next_discriminating_question", "evidence_needed", "change_type",
            "candidate_expression",
        ):
            if key in metadata:
                entry[key] = metadata[key]

    def top_next(self, n=5):
        return project_top_next(self.next, n)

    def next_with_fields(self, now_round=None):
        """Highest-priority actionable idea, excluding stale work items.

        ``next`` is a bounded queue, not durable evidence.  A high priority
        suggestion from an old round must not revive a closed/redundant field
        family merely because newer research has not rewritten the same text.
        """
        return project_next_with_fields(
            self.next, now_round, self.next_max_age_rounds
        )

    # ------------------------------------------------------------- lineage

    def lineage_decision(self, lineage_id):
        return (self.lineages.get(lineage_id) or {}).get("decision", "CONTINUE")

    def record_lineage_result(self, lineage_id, score, label, round_no,
                              counts_toward_stop=True, source_key=None,
                              experiment_id=None):
        """Update CONTINUE / STOP / KILL from resolved research evidence.

        A material score improvement earns another experiment.  Two resolved,
        non-gaining experiments STOP a lineage; a third KILLs it.  UNKNOWN and
        system failures never call this method, so they cannot close research.

        Per the 2026-08-22 user policy, the no-gain STOP/KILL discipline only
        applies to lineages that already reached a submittable standard;
        callers pass ``counts_toward_stop=False`` for promising-but-unqualified
        results so those lineages keep iterating with a different variable
        class instead of being closed by count alone (the observation is still
        recorded via last_label/last_round).
        """
        if not lineage_id:
            return "CONTINUE"
        entry = self.lineages.setdefault(lineage_id, {
            "best_score": None, "no_gain_streak": 0, "decision": "CONTINUE",
        })
        if source_key:
            records = entry.setdefault("settlement_results", [])
            source_key = str(source_key)
            for record in records:
                if record.get("source_key") == source_key:
                    return entry.get("decision", "CONTINUE")
            record = {
                "source_key": source_key, "experiment_id": experiment_id,
                "score": score, "label": label, "round": round_no,
                "counts_toward_stop": bool(counts_toward_stop),
            }
            replaced = False
            if experiment_id:
                for index, previous in enumerate(records):
                    if previous.get("experiment_id") == experiment_id:
                        records[index] = record
                        replaced = True
                        break
            if not replaced:
                records.append(record)
            # Bound only opaque replay metadata and rebuild the reducer so a
            # revised final settlement replaces, rather than double-counts.
            del records[:-self.max_lineages]
            best_score = None
            no_gain = 0
            decision = "CONTINUE"
            for item in records:
                item_score = item.get("score")
                if item_score is not None and (
                    best_score is None or item_score > best_score + 0.05
                ):
                    best_score = item_score
                    no_gain = 0
                    decision = "CONTINUE"
                elif item.get("counts_toward_stop"):
                    no_gain += 1
                    decision = (
                        "KILL" if no_gain >= 3
                        else "STOP" if no_gain >= 2
                        else decision
                    )
            entry["best_score"] = best_score
            entry["no_gain_streak"] = no_gain
            entry["decision"] = decision
            entry["last_label"] = label
            entry["last_round"] = round_no
            return decision
        best = entry.get("best_score")
        improved = score is not None and (best is None or score > best + 0.05)
        if improved:
            entry["best_score"] = score
            entry["no_gain_streak"] = 0
            entry["decision"] = "CONTINUE"
        elif counts_toward_stop:
            entry["no_gain_streak"] = entry.get("no_gain_streak", 0) + 1
            if entry["no_gain_streak"] >= 3:
                entry["decision"] = "KILL"
            elif entry["no_gain_streak"] >= 2:
                entry["decision"] = "STOP"
        entry["last_label"] = label
        entry["last_round"] = round_no
        return entry["decision"]

    # ---------------------------------------------------------- hypotheses

    def register_hypothesis(self, hypothesis):
        """Mark a hypothesis as attempted (used) and track it as active."""
        hyp_id = hypothesis.get("id")
        if hyp_id:
            self.used_hypotheses.add(str(hyp_id))
            self._trim_used_hypotheses()
        for entry in self.active_hypotheses:
            if entry["id"] == hyp_id:
                entry["last_round"] = hypothesis.get("_round", entry.get("last_round"))
                return entry
        entry = {
            "id": hyp_id or uuid.uuid4().hex[:8],
            "statement": hypothesis.get("statement", ""),
            "tags": list(hypothesis.get("tags") or []),
            "direction": hypothesis.get("direction"),
            "datasets": list(hypothesis.get("datasets") or hypothesis.get("dataset_hints") or []),
            "status": "active",
            "outcome": "INCONCLUSIVE",
            "last_round": hypothesis.get("_round"),
            "last_verdict": None,
        }
        self.active_hypotheses.append(entry)
        return entry

    def mark_hypothesis(self, hyp_id, verdict, round_no, outcome=None,
                        confirmation=None, source_key=None):
        """Keep legacy execution status and a separate research outcome."""
        for entry in self.active_hypotheses:
            if entry["id"] == hyp_id:
                semantic = {"hyp_id": hyp_id, "verdict": verdict,
                            "round_no": round_no, "outcome": outcome,
                            "confirmation": confirmation}
                replay = self._source_replay(source_key, "hypothesis", semantic)
                if replay is not None:
                    return replay
                entry["status"] = verdict
                entry["last_verdict"] = verdict
                if outcome in {"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"}:
                    entry["outcome"] = outcome
                if isinstance(confirmation, dict):
                    for key in (
                        "evidence_refs", "lineages", "independent_lineages",
                        "confirmation_status", "agent_interpretation",
                        "outcome_reason",
                    ):
                        if key not in confirmation:
                            continue
                        value = confirmation[key]
                        if isinstance(value, list):
                            entry[key] = list(value)
                        elif isinstance(value, dict):
                            entry[key] = dict(value)
                        elif isinstance(value, str):
                            entry[key] = value
                entry["last_round"] = round_no
                self._stamp_source(entry, source_key, semantic)
                return entry
        return None

    # ---------------------------------------------------- expression dedupe

    def remember_expression(self, expression):
        canonical = canonical_expression(expression)
        if canonical:
            self.seen_expressions.add(canonical)
            self._trim_seen_expressions()

    def forget_expression(self, expression):
        """Keep UNKNOWN/system failures retryable across restarts."""
        self.seen_expressions.discard(canonical_expression(expression))

    def is_seen(self, expression):
        return canonical_expression(expression) in self.seen_expressions

    def _trim_seen_expressions(self):
        """Keep only a deterministic warm cache, not a second full ledger.

        Exact historical dedupe is performed by Agent against the append-only
        trajectory.  This set is only a bounded startup/proposal hint, so it
        must not grow with an all-day factory session.
        """
        if len(self.seen_expressions) > self.max_seen_expressions:
            self.seen_expressions = set(
                sorted(self.seen_expressions)[-self.max_seen_expressions:]
            )

    def _trim_used_hypotheses(self):
        """Keep the hypothesis avoidance cache bounded and deterministic."""
        if len(self.used_hypotheses) > self.max_used_hypotheses:
            self.used_hypotheses = set(
                sorted(self.used_hypotheses)[-self.max_used_hypotheses:]
            )


    def set_current_best(self, experiment):
        self.current_best = experiment.to_dict()

    def set_current_best_record(self, record):
        """Persist a validated candidate snapshot supplied by an aggregator."""
        if not isinstance(record, dict):
            raise TypeError("current_best record must be an object")
        self.current_best = dict(record)

    # ------------------------------------------------------------ context

    def context(self, recent_experiments=None, short_term_n=5, garbage_n=3):
        """The compressed view the model reads — requirement #8. Long-term
        tiers (best / hypotheses / lessons / avoid / next) plus a small
        window of short-term entries and a garbage digest for audit."""
        lessons = sorted(
            self.lessons, key=lambda x: -x.get("evidence", 0)
        )[: self.max_lessons]
        avoid = sorted(self.avoid, key=lambda x: -x.get("updated", 0))[: self.max_avoid]
        next_ideas = self.top_next(self.max_next)
        garbage = self.garbage[-garbage_n:] if self.garbage else []
        supported, contradicted, unresolved, unresolved_mechanisms, discriminating = (
            self._learning_context()
        )
        return {
            "current_best": self.current_best,
            "active_hypotheses": [
                {k: e[k] for k in (
                    "id", "statement", "status", "outcome", "last_round",
                    "last_verdict", "evidence_refs", "lineages",
                    "independent_lineages", "confirmation_status",
                    "agent_interpretation", "outcome_reason",
                ) if k in e}
                for e in self.active_hypotheses
            ][: self.max_hypotheses],
            "recent_key_experiments": recent_experiments or [],
            "short_term": self.recent_short_term(short_term_n),
            "garbage_digest": {
                "stats": self.garbage_stats(),
                "recent": garbage,
            },
            "lessons": lessons,
            "avoid": avoid,
            "next": next_ideas,
            "supported_mechanisms": supported,
            "contradicted_mechanisms": contradicted,
            "unresolved_questions": unresolved,
            "unresolved_mechanisms": unresolved_mechanisms,
            "next_discriminating_questions": discriminating,
        }

    def _learning_context(self):
        """Project bounded mechanism learning without copying raw evidence."""
        return learning_context(
            self.lessons + self.short_term,
            self.lessons + self.short_term + self.next,
        )

    # ----------------------------------------------------------- compress

    def compress(self):
        # Persisted/AI-authored rows are untrusted input.  Keep only the
        # minimal shape each tier's consumers can interpret, so one malformed
        # dictionary cannot abort an unattended factory save or create a
        # duplicate garbage stream on every retry.
        self.lessons = [
            item for item in dict_list(self.lessons)
            if isinstance(item.get("claim"), str) and item["claim"].strip()
        ]
        self.avoid = [
            item for item in dict_list(self.avoid)
            if isinstance(item.get("direction"), str) and item["direction"].strip()
        ]
        self.next = [
            item for item in dict_list(self.next)
            if isinstance(item.get("idea"), str) and item["idea"].strip()
        ]
        self.active_hypotheses = [
            item for item in dict_list(self.active_hypotheses)
            if isinstance(item.get("id"), (str, int)) and str(item["id"]).strip()
        ]
        self.short_term = [
            item for item in dict_list(self.short_term)
            if isinstance(item.get("text"), str) and item["text"].strip()
        ]
        merged = []
        for lesson in sorted(
            self.lessons, key=lambda x: -self._number(x.get("evidence"))
        ):
            if not any(similar(lesson["claim"], m["claim"], threshold=0.75) for m in merged):
                merged.append(lesson)
        self.lessons = merged[: self.max_lessons]
        self.avoid = sorted(
            self.avoid, key=lambda x: -self._number(x.get("updated"))
        )[: self.max_avoid]
        self.next = sorted(
            self.next, key=lambda x: -self._number(x.get("priority"))
        )[: self.max_next]
        self.active_hypotheses = sorted(
            self.active_hypotheses, key=lambda x: -self._number(x.get("last_round"))
        )[: self.max_hypotheses]
        self.short_term = sorted(
            self.short_term,
            key=lambda x: (
                -self._number(x.get("updated")),
                -self._number(x.get("round")),
            ),
        )[: self.max_short_term]
        if len(self.lineages) > self.max_lineages:
            ordered = sorted(
                self.lineages.items(),
                key=lambda item: (
                    self._number(item[1].get("last_round"))
                    if isinstance(item[1], dict) else 0,
                    str(item[0]),
                ),
                reverse=True,
            )
            self.lineages = dict(ordered[: self.max_lineages])
        if len(self.garbage) > self.max_garbage:
            self.garbage = sorted(
                dict_list(self.garbage),
                key=lambda x: self._number(x.get("moved_at")),
                reverse=True,
            )[: self.max_garbage]
        self.seen_expressions = set(self.seen_expressions)
        self._trim_seen_expressions()
        self.used_hypotheses = {
            str(value) for value in self.used_hypotheses
            if value and not _EPHEMERAL_HYPOTHESIS_ID.fullmatch(str(value))
        }
        self._trim_used_hypotheses()

    # ------------------------------------------------------------- helpers

