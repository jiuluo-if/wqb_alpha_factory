"""Pure source-replay reducers for the ExperienceMemory owner."""

from __future__ import annotations

import hashlib

from .memory_codec import stable_payload


class MemorySourceReplayConflict(ValueError):
    """A durable memory source key was reused for different semantics."""

    code = "MEMORY_SOURCE_REPLAY_CONFLICT"


class MemorySourceReplayUnverifiable(ValueError):
    """A compacted source may have been seen but its exact receipt is gone."""

    code = "MEMORY_REPLAY_UNVERIFIABLE"


_BLOOM_BITS = 256
_BLOOM_HASHES = 3


def _source_digest(source_key):
    return hashlib.sha256(str(source_key).encode("utf-8")).hexdigest()


def _bloom_positions(source_key):
    digest = _source_digest(source_key)
    return tuple(
        int(digest[index:index + 8], 16) % _BLOOM_BITS
        for index in range(0, _BLOOM_HASHES * 8, 8)
    )


def _bloom_value(metadata):
    if not isinstance(metadata, dict):
        return 0
    if metadata.get("version") != 1:
        return 0
    try:
        return int(metadata.get("bits", "0"), 16)
    except (TypeError, ValueError):
        return 0


def _bloom_contains(metadata, source_key):
    bits = _bloom_value(metadata)
    return bits and all(bits & (1 << position) for position in _bloom_positions(source_key))


def _merge_compaction(left, right):
    left_bits = _bloom_value(left)
    right_bits = _bloom_value(right)
    if not left_bits and not right_bits:
        return None
    return {
        "version": 1,
        "bits": format(left_bits | right_bits, "x"),
        "hashes": _BLOOM_HASHES,
        "bit_count": _BLOOM_BITS,
        "compacted_count": int((left or {}).get("compacted_count", 0) or 0)
        + int((right or {}).get("compacted_count", 0) or 0),
        "opaque_digest": hashlib.sha256(
            f"{(left or {}).get('opaque_digest', '')}|{(right or {}).get('opaque_digest', '')}"
            .encode()
        ).hexdigest()[:16],
    }


def _compact_metadata(receipts):
    if not receipts:
        return None
    bits = 0
    keys = []
    for receipt in receipts:
        if not isinstance(receipt, dict) or not receipt.get("source_key"):
            continue
        key = str(receipt["source_key"])
        keys.append(key)
        for position in _bloom_positions(key):
            bits |= 1 << position
    if not keys:
        return None
    return {
        "version": 1,
        "bits": format(bits, "x"),
        "hashes": _BLOOM_HASHES,
        "bit_count": _BLOOM_BITS,
        "compacted_count": len(keys),
        "opaque_digest": hashlib.sha256(
            "|".join(sorted(_source_digest(key) for key in keys)).encode("utf-8")
        ).hexdigest()[:16],
    }


def find_source_entry(source_key, stores, garbage):
    """Find a source receipt across live tiers and bounded tombstones."""
    if not source_key:
        return None
    key = str(source_key)
    for kind, entries in stores:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            receipts = entry.get("_source_receipts") or []
            if entry.get("source_key") == key and (
                not receipts or any(
                    isinstance(receipt, dict) and receipt.get("source_key") == key
                    for receipt in receipts
                )
            ):
                return kind, entry
            if any(isinstance(receipt, dict) and receipt.get("source_key") == key
                   for receipt in receipts):
                return kind, entry
    for tomb in garbage:
        entry = tomb.get("entry") if isinstance(tomb, dict) else None
        if not isinstance(entry, dict):
            continue
        receipts = entry.get("_source_receipts") or []
        if (entry.get("source_key") == key and (
            not receipts or any(
                isinstance(receipt, dict) and receipt.get("source_key") == key
                for receipt in receipts
            )
        )) or any(
            isinstance(receipt, dict) and receipt.get("source_key") == key
            for receipt in receipts
        ):
            return tomb.get("kind", "garbage"), entry
    return None


def replay_source(source_key, kind, semantic, stores, garbage):
    """Return an existing entry for an exact replay or raise on conflict."""
    found = find_source_entry(source_key, stores, garbage)
    if found is None:
        compatible_kinds = {kind}
        if kind in {"short_term", "lesson"}:
            compatible_kinds.update({"short_term", "lesson"})
        for store_kind, entries in stores:
            if store_kind not in compatible_kinds:
                continue
            if any(
                _bloom_contains(entry.get("_source_receipt_compaction"), source_key)
                for entry in entries if isinstance(entry, dict)
            ):
                raise MemorySourceReplayUnverifiable(
                    f"{MemorySourceReplayUnverifiable.code}: source_key_digest={_source_digest(source_key)[:16]}"
                )
        for tomb in garbage:
            if not isinstance(tomb, dict) or tomb.get("kind") not in compatible_kinds:
                continue
            entry = tomb.get("entry")
            if isinstance(entry, dict) and _bloom_contains(
                entry.get("_source_receipt_compaction"), source_key
            ):
                raise MemorySourceReplayUnverifiable(
                    f"{MemorySourceReplayUnverifiable.code}: source_key_digest={_source_digest(source_key)[:16]}"
                )
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
    evicted = receipts[:-limit]
    result["_source_receipts"] = receipts[-limit:]
    compacted = _compact_metadata(evicted)
    if compacted:
        result["_source_receipt_compaction"] = _merge_compaction(
            result.get("_source_receipt_compaction"), compacted
        )
    return result


def merge_receipt_provenance(target, source, limit):
    """Merge bounded receipts and compaction proof during tier promotion."""
    result = dict(target)
    existing = [
        receipt for receipt in result.get("_source_receipts", [])
        if isinstance(receipt, dict) and receipt.get("source_key")
    ]
    incoming = [
        receipt for receipt in source.get("_source_receipts", [])
        if isinstance(receipt, dict) and receipt.get("source_key")
    ] if isinstance(source, dict) else []
    combined = {receipt["source_key"]: receipt for receipt in existing + incoming}
    evicted = list(combined.values())[:-limit]
    result["_source_receipts"] = list(combined.values())[-limit:]
    compacted = _compact_metadata(evicted)
    result["_source_receipt_compaction"] = _merge_compaction(
        _merge_compaction(
            result.get("_source_receipt_compaction"),
            source.get("_source_receipt_compaction") if isinstance(source, dict) else None,
        ),
        compacted,
    )
    if result.get("_source_receipt_compaction") is None:
        result.pop("_source_receipt_compaction", None)
    return result
