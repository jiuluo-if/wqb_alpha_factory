"""Durable checkpoint persistence and conservative unfinished-round scans."""

from __future__ import annotations

import json
import os
import re
import threading
import time

from .artifacts import atomic_write_json_if_changed
from .locking import StateMutationDelegation, single_instance_scope
from .schema import CHECKPOINT_VERSION, CREATED_BY_VERSION, migrate_artifact

_CHECKPOINT_NAME = re.compile(r"round_(\d+)\.checkpoint\.json")
_REQUIRED_EXPERIMENT_FIELDS = {
    "id", "round", "hypothesis_id", "expression", "settings",
    "fields_used", "status",
}


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
            keep = {
                "schema_version", "created_by_version", "id", "round",
                "hypothesis_id", "expression", "settings", "fields_used",
                "status", "proposal_id", "submission_fingerprint",
                "optimization_decision_id",
                "submission_started_at", "progress_url", "experiment_stage",
                "research_role", "change_type", "lineage_id", "template_id",
                "template_family", "proposal_origin", "research_layer",
                "template_version", "template_mode", "template_branch_of",
                "template_fingerprint", "template_structural_fingerprint",
                "template_mechanism_fingerprint", "operator_role",
                "operator_role_mapping", "operator_realization_fingerprint",
                "operator_capability_fingerprint",
            }
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
    def _validate(round_no, data):
        data = migrate_artifact("checkpoint", data)
        if (
            not isinstance(data, dict)
            or data.get("round_no") != int(round_no)
            or not isinstance(data.get("experiments"), list)
            or not isinstance(data.get("hypothesis"), dict)
        ):
            return None
        for row in data["experiments"]:
            if not isinstance(row, dict) or not _REQUIRED_EXPERIMENT_FIELDS.issubset(row):
                return None
            if (
                not isinstance(row["id"], (str, int))
                or not str(row["id"]).strip()
                or not isinstance(row["expression"], str)
                or not row["expression"].strip()
                or not isinstance(row["settings"], dict)
                or not isinstance(row["fields_used"], (list, tuple))
                or not isinstance(row["status"], str)
            ):
                return None
        return data

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
        result = None
        result_round = None
        for record in self.scan():
            checkpoint_round = record["round_no"]
            if checkpoint_round == int(round_no):
                continue
            if result_round is not None and checkpoint_round >= result_round:
                continue
            if record["malformed"] or not record["checkpoint"].get("complete", False):
                result = record["path"]
                result_round = checkpoint_round
        return result

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
            try:
                checkpoint = self._validate(round_no, decoded) if readable else None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                checkpoint = None
            raw = decoded if readable and isinstance(decoded, dict) else {}
            records.append({
                "path": path,
                "round_no": round_no,
                "checkpoint": checkpoint if checkpoint is not None else raw,
                "malformed": checkpoint is None,
            })
        return records
