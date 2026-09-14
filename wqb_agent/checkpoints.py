"""Durable checkpoint persistence and conservative unfinished-round scans."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict

from .artifacts import atomic_write_json_if_changed
from .expression import submission_fingerprint
from .locking import StateMutationDelegation, single_instance_scope
from .schema import CHECKPOINT_VERSION, CREATED_BY_VERSION, migrate_artifact
from .state import (
    IDENTITY_FIELDS,
    OPERATOR_PROVENANCE_FIELDS,
    TERMINAL_STATUSES,
    UNRESOLVED_STATUSES,
)

_CHECKPOINT_NAME = re.compile(r"round_(\d+)\.checkpoint\.json")
_REQUIRED_EXPERIMENT_FIELDS = {
    "id", "round", "hypothesis_id", "expression", "settings",
    "fields_used", "status",
}
_CHECKPOINT_IDENTITY_FIELDS = tuple(dict.fromkeys(
    (*IDENTITY_FIELDS, *OPERATOR_PROVENANCE_FIELDS,
     "template_id", "template_family", "template_stage_path", "template_ref",
     "template_slots", "experiment_stage", "research_role", "change_type",
     "proposal_origin", "research_layer", "progress_url")
))
_LEGAL_EXPERIMENT_STATUSES = UNRESOLVED_STATUSES | TERMINAL_STATUSES


class CheckpointStore:
    """Own only checkpoint file mechanics; never submits or appends trajectory."""

    def __init__(self, state_dir, lock=None):
        self.state_dir = state_dir
        self._lock = lock or threading.Lock()

    def path(self, round_no):
        return os.path.join(self.state_dir, f"round_{int(round_no)}.checkpoint.json")

    def write(self, round_no, hypothesis, experiments, complete, *, delegation=None):
        """Atomically persist one checkpoint and return whether bytes changed."""
        state_dir = os.path.abspath(self.state_dir)
        if delegation is None:
            owner_scope = single_instance_scope(state_dir, operation="checkpoint-write")
        else:
            if not isinstance(delegation, StateMutationDelegation):
                raise TypeError("delegation must be a StateMutationDelegation")
            owner_scope = delegation.authorization(state_dir)
        with owner_scope:
            return self._write_owned(round_no, hypothesis, experiments, complete)

    def _write_owned(self, round_no, hypothesis, experiments, complete):
        path = self.path(round_no)
        os.makedirs(self.state_dir, exist_ok=True)
        checkpoint_experiments = []
        for exp in experiments:
            row = exp.to_dict()
            # A checkpoint is a recovery/dedupe boundary, not a local result
            # archive.  This applies to unfinished checkpoints too: retain
            # only enough identity and known progress URL to continue a
            # transport recovery, while metrics, checks, Alpha IDs, and
            # result-side evidence remain in the process-local daily cache.
            keep = {"schema_version", "created_by_version", "status",
                    *_CHECKPOINT_IDENTITY_FIELDS}
            row = {key: value for key, value in row.items() if key in keep}
            checkpoint_experiments.append(row)
        data = {
            "schema_version": CHECKPOINT_VERSION,
            "created_by_version": CREATED_BY_VERSION,
            "round_no": int(round_no),
            "hypothesis": hypothesis,
            "experiments": checkpoint_experiments,
            "complete": bool(complete),
            "updated_at": time.time(),
        }
        with self._lock:
            return atomic_write_json_if_changed(
                path, data, ignored_keys=("updated_at",)
            )

    @staticmethod
    def _read_decoded(path):
        try:
            with open(path, encoding="utf-8-sig") as handle:
                return True, json.load(handle)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False, None

    @staticmethod
    def _validate_with_code(round_no, data):
        if not isinstance(data, dict):
            return None, "CHECKPOINT_MALFORMED"
        try:
            expected_round = int(round_no)
        except (TypeError, ValueError):
            return None, "CHECKPOINT_ROUND_IDENTITY_MISMATCH"
        if isinstance(round_no, bool) or expected_round <= 0:
            return None, "CHECKPOINT_ROUND_IDENTITY_MISMATCH"
        stored_version = data.get("schema_version")
        try:
            future_version = int(stored_version)
        except (TypeError, ValueError):
            future_version = None
        if future_version is not None and future_version > CHECKPOINT_VERSION:
            return None, "UNSUPPORTED_FUTURE_CHECKPOINT_SCHEMA"
        data = migrate_artifact("checkpoint", data)
        if (
            isinstance(data.get("round_no"), bool)
            or not isinstance(data.get("round_no"), int)
            or data.get("round_no") <= 0
            or data.get("round_no") != expected_round
        ):
            return None, "CHECKPOINT_ROUND_IDENTITY_MISMATCH"
        if (
            not isinstance(data.get("experiments"), list)
            or not isinstance(data.get("hypothesis"), dict)
        ):
            return None, "CHECKPOINT_MALFORMED"
        if not isinstance(data.get("complete"), bool):
            return None, "CHECKPOINT_COMPLETE_TYPE_INVALID"
        hypothesis_id = data["hypothesis"].get("id")
        if hypothesis_id not in (None, "") and not isinstance(hypothesis_id, (str, int)):
            return None, "CHECKPOINT_HYPOTHESIS_IDENTITY_INVALID"
        seen_ids = set()
        seen_fingerprints = set()
        seen_proposal_ids = set()
        seen_progress = {}
        unresolved = False
        for row in data["experiments"]:
            if not isinstance(row, dict) or not _REQUIRED_EXPERIMENT_FIELDS.issubset(row):
                return None, "CHECKPOINT_REQUIRED_IDENTITY_MISSING"
            row_round = row.get("round")
            if (
                isinstance(row_round, bool)
                or not isinstance(row_round, int)
                or row_round <= 0
                or row_round != expected_round
            ):
                return None, "CHECKPOINT_ROUND_IDENTITY_MISMATCH"
            if (
                not isinstance(row["id"], (str, int))
                or isinstance(row["id"], bool)
                or not str(row["id"]).strip()
                or not isinstance(row["expression"], str)
                or not row["expression"].strip()
                or not isinstance(row["settings"], dict)
                or not isinstance(row["fields_used"], (list, tuple))
                or not isinstance(row["status"], str)
            ):
                return None, "CHECKPOINT_REQUIRED_IDENTITY_INVALID"
            if hypothesis_id not in (None, "") and row.get("hypothesis_id") != hypothesis_id:
                return None, "CHECKPOINT_HYPOTHESIS_IDENTITY_MISMATCH"
            status = row["status"]
            if status not in _LEGAL_EXPERIMENT_STATUSES:
                return None, "CHECKPOINT_STATUS_INVALID"
            unresolved = unresolved or status in UNRESOLVED_STATUSES
            experiment_id = str(row["id"])
            if experiment_id in seen_ids:
                return None, "CHECKPOINT_DUPLICATE_EXPERIMENT_ID"
            seen_ids.add(experiment_id)
            fingerprint = submission_fingerprint(row["expression"], row["settings"])
            if fingerprint in seen_fingerprints:
                return None, "CHECKPOINT_DUPLICATE_SUBMISSION_IDENTITY"
            seen_fingerprints.add(fingerprint)
            proposal_id = row.get("proposal_id")
            if proposal_id not in (None, "", [], {}):
                proposal_key = str(proposal_id)
                if proposal_key in seen_proposal_ids:
                    return None, "CHECKPOINT_DUPLICATE_PROPOSAL_ID"
                seen_proposal_ids.add(proposal_key)
            stored = row.get("submission_fingerprint")
            if stored not in (None, ""):
                if str(stored) != fingerprint:
                    return None, "CHECKPOINT_SUBMISSION_IDENTITY_MISMATCH"
            progress_url = row.get("progress_url")
            if progress_url not in (None, ""):
                progress_key = str(progress_url)
                prior = seen_progress.get(progress_key)
                if prior is not None and prior != (fingerprint, experiment_id):
                    return None, "CHECKPOINT_PROGRESS_IDENTITY_COLLISION"
                seen_progress[progress_key] = (fingerprint, experiment_id)
        if data["complete"] and unresolved:
            return None, "CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION"
        return data, None

    @staticmethod
    def _validate(round_no, data):
        checkpoint, _ = CheckpointStore._validate_with_code(round_no, data)
        return checkpoint

    def load(self, round_no):
        """Load a validated checkpoint; malformed input returns ``None``."""
        readable, raw = self._read_decoded(self.path(round_no))
        if not readable:
            return None
        try:
            return self._validate(round_no, raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def unfinished_except(self, round_no):
        """Return the lowest-round unfinished or malformed checkpoint path."""
        record = self.unfinished_record_except(round_no)
        return record["path"] if record else None

    def unfinished_record_except(self, round_no, *, records=None):
        """Return the lowest-round unfinished or malformed checkpoint record."""
        result = None
        result_round = None
        for record in self.scan() if records is None else records:
            checkpoint_round = record["round_no"]
            if checkpoint_round == int(round_no):
                continue
            if result_round is not None and checkpoint_round >= result_round:
                continue
            if record["malformed"] or record["checkpoint"].get("complete") is not True:
                result = record
                result_round = checkpoint_round
        return result

    @staticmethod
    def _submission_fingerprint(row):
        expression = row.get("expression")
        settings = row.get("settings")
        if isinstance(expression, str) and isinstance(settings, dict):
            # The payload is the canonical source for the existing execution
            # fingerprint.  This also prevents a malformed legacy field from
            # silently weakening the unresolved identity fence.
            return submission_fingerprint(expression, settings)
        value = row.get("submission_fingerprint")
        if value not in (None, ""):
            return str(value)
        return None

    def unresolved_submission_identities(self, *, exclude_round=None, records=None):
        """Return unresolved execution keys from all incomplete checkpoints.

        The returned values are bounded local identity metadata only.  The
        helper is read-only and reconstructs the key for legacy rows that did
        not persist ``submission_fingerprint``.
        """
        unresolved = defaultdict(list)
        excluded = int(exclude_round) if exclude_round is not None else None
        source = self.scan() if records is None else records
        for record in source:
            if record["malformed"]:
                continue
            checkpoint = record["checkpoint"]
            if checkpoint.get("complete") is True or (
                excluded is not None and record["round_no"] == excluded
            ):
                continue
            for row in checkpoint.get("experiments") or ():
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "").upper() not in {
                    "PENDING", "RUNNING", "SUBMITTING", "UNKNOWN", "SUBMIT_UNKNOWN",
                }:
                    continue
                fingerprint = self._submission_fingerprint(row)
                if fingerprint:
                    unresolved[fingerprint].append({
                        "round_no": record["round_no"],
                        "experiment_id": str(row.get("id")),
                    })
        return dict(unresolved)

    def scan(self, names=None):
        """Return the authoritative read-only view of checkpoint files.

        Every consumer uses the same filename rule and ``load`` validation;
        malformed files remain visible so callers can fail closed.
        """
        if names is None:
            try:
                names = os.listdir(self.state_dir)
            except OSError:
                return []
        records = []
        for name in sorted(names):
            match = _CHECKPOINT_NAME.fullmatch(name)
            if not match:
                continue
            round_no = int(match.group(1))
            path = os.path.join(self.state_dir, name)
            readable, decoded = self._read_decoded(path)
            validation_code = None
            try:
                checkpoint, validation_code = (
                    self._validate_with_code(round_no, decoded)
                    if readable else (None, "CHECKPOINT_UNREADABLE")
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                checkpoint = None
                validation_code = "CHECKPOINT_MALFORMED"
            raw = decoded if readable and isinstance(decoded, dict) else {}
            records.append({
                "path": path,
                "round_no": round_no,
                "checkpoint": checkpoint if checkpoint is not None else raw,
                "malformed": checkpoint is None,
                "validation_code": validation_code,
            })
        return records
