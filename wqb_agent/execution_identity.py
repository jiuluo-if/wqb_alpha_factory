"""Transient exact proposal-to-execution identity projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class ExecutionBindingIndex:
    """Cache durable bindings for one workflow owner lifecycle.

    The index is reconstructible and never persisted.  Callers provide an
    owner-generation signature; a changed signature forces a fresh projection.
    """

    def __init__(self):
        self._signature = None
        self._bindings: dict[str, set[str]] = {}

    def refresh(
        self,
        signature,
        trajectory_rows: Iterable[Mapping[str, object]],
        checkpoint_records: Iterable[Mapping[str, object]],
        ledger_bindings: Mapping[str, Iterable[str]],
    ) -> dict[str, set[str]]:
        if signature != self._signature:
            bindings: dict[str, set[str]] = {}

            def add(proposal_id, fingerprint):
                if proposal_id in (None, "") or fingerprint in (None, ""):
                    return
                bindings.setdefault(str(proposal_id), set()).add(str(fingerprint))

            for row in trajectory_rows:
                if isinstance(row, Mapping):
                    add(row.get("proposal_id"), row.get("submission_fingerprint"))
            for record in checkpoint_records:
                if not isinstance(record, Mapping) or record.get("malformed"):
                    continue
                checkpoint = record.get("checkpoint")
                if not isinstance(checkpoint, Mapping):
                    continue
                for row in checkpoint.get("experiments") or ():
                    if isinstance(row, Mapping):
                        add(row.get("proposal_id"), row.get("submission_fingerprint"))
            for proposal_id, fingerprints in (ledger_bindings or {}).items():
                for fingerprint in fingerprints:
                    add(proposal_id, fingerprint)
            self._bindings = bindings
            self._signature = signature
        return {key: set(values) for key, values in self._bindings.items()}

    def is_current(self, signature) -> bool:
        return signature == self._signature

    def snapshot(self) -> dict[str, set[str]]:
        return {key: set(values) for key, values in self._bindings.items()}
