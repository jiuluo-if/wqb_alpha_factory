"""Pure source-replay reducers for the ExperienceMemory owner."""

from __future__ import annotations

from .memory_codec import stable_payload


class MemorySourceReplayConflict(ValueError):
    """A durable memory source key was reused for different semantics."""

    code = "MEMORY_SOURCE_REPLAY_CONFLICT"


def find_source_entry(source_key, stores, garbage):
    """Find a source receipt across live tiers and bounded tombstones."""
    if not source_key:
        return None
    key = str(source_key)
    for kind, entries in stores:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("source_key") == key:
                return kind, entry
            if any(
                isinstance(receipt, dict) and receipt.get("source_key") == key
                for receipt in entry.get("_source_receipts", [])
            ):
                return kind, entry
    for tomb in garbage:
        entry = tomb.get("entry") if isinstance(tomb, dict) else None
        if not isinstance(entry, dict):
            continue
        if entry.get("source_key") == key or any(
            isinstance(receipt, dict) and receipt.get("source_key") == key
            for receipt in entry.get("_source_receipts", [])
        ):
            return tomb.get("kind", "garbage"), entry
    return None


def replay_source(source_key, kind, semantic, stores, garbage):
    """Return an existing entry for an exact replay or raise on conflict."""
    found = find_source_entry(source_key, stores, garbage)
    if found is None:
        return None
    old_kind, old = found
    expected = stable_payload(semantic)
    receipts = old.get("_source_receipts") or []
    old_semantic = next(
        (
            receipt.get("_source_semantic")
            for receipt in receipts
            if isinstance(receipt, dict)
            and receipt.get("source_key") == str(source_key)
        ),
        old.get("_source_semantic"),
    )
    cross_tier_replay = (
        {old_kind, kind} == {"lesson", "short_term"}
        and old_semantic == expected
    )
    if (old_kind != kind and not cross_tier_replay) or (
        old_kind == kind and old_semantic != expected
    ):
        raise MemorySourceReplayConflict(
            f"MEMORY_SOURCE_REPLAY_CONFLICT: source_key={source_key}"
        )
    return old


def source_receipt(entry, source_key, semantic, limit):
    """Return an entry copy with one bounded canonical source receipt."""
    result = dict(entry)
    if not source_key:
        return result
    key = str(source_key)
    payload = stable_payload(semantic)
    result.setdefault("source_key", key)
    result.setdefault("_source_semantic", payload)
    receipts = [
        receipt
        for receipt in result.get("_source_receipts", [])
        if isinstance(receipt, dict) and receipt.get("source_key") != key
    ]
    receipts.append({"source_key": key, "_source_semantic": payload})
    result["_source_receipts"] = receipts[-limit:]
    return result
