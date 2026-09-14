"""Idempotent persistence primitives for the long-running factory.

The research state has different sources of truth, so this module deliberately
only owns *file replacement mechanics*.  It does not decide which state may be
written and it never appends to trajectory evidence.  Callers provide the
already validated payload and get a no-op when the durable content is
unchanged.
"""

import hashlib
import json
import os
import threading
import time
from contextlib import nullcontext


def _without_keys(value, ignored):
    if not ignored:
        return value
    if isinstance(value, dict):
        return {k: _without_keys(v, ignored) for k, v in value.items()
                if k not in ignored}
    if isinstance(value, list):
        return [_without_keys(v, ignored) for v in value]
    return value


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def iter_jsonl_objects(path, *, stats=None):
    """Yield valid JSON object rows from a rebuildable JSONL artifact.

    Readers of derived files share this boundary so malformed or non-object
    rows cannot make one unattended report behave differently from another.
    The generator owns the file handle and closes it when exhausted or closed
    by the caller.
    """
    if not path:
        return
    try:
        handle = open(path, encoding="utf-8-sig")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                if isinstance(stats, dict):
                    stats["invalid_rows"] = stats.get("invalid_rows", 0) + 1
                continue
            if isinstance(row, dict):
                yield row
            elif isinstance(stats, dict):
                stats["invalid_rows"] = stats.get("invalid_rows", 0) + 1


def atomic_write_text_if_changed(path, text):
    """Atomically replace a text file only when its bytes have changed."""
    encoded = str(text).encode("utf-8")
    try:
        with open(path, "rb") as handle:
            if handle.read() == encoded:
                return False
    except OSError:
        pass
    return _atomic_replace(path, encoded)


def atomic_write_json_if_changed(path, payload, *, ignored_keys=(),
                                 indent=2, ensure_ascii=False,
                                 sort_keys=False):
    """Persist JSON with stable comparison and collision-safe temp files.

    ``ignored_keys`` is for volatile bookkeeping such as ``updated_at``.  A
    volatile-only change is intentionally a no-op; timestamps must describe a
    durable state transition, not a polling heartbeat.
    """
    existing = _read_json(path)
    if existing is not None and _without_keys(existing, set(ignored_keys)) == _without_keys(
        payload, set(ignored_keys)
    ):
        return False
    serialized = json.dumps(
        payload, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys
    ) + "\n"
    return _atomic_replace(path, serialized.encode("utf-8"))


def append_jsonl_best_effort(path, payload, identity_keys, *, lock=None):
    """Append an audit record with optional caller-owned uniqueness locking.

    With a lock shared by all callers of the owning component, the identity
    check and append are atomic for those callers.  Without a lock this is
    explicitly best-effort and provides no concurrent or cross-process
    uniqueness guarantee.  Append-only research evidence such as
    ``trajectory.jsonl`` must use its own state API instead.
    """
    if not isinstance(payload, dict) or not identity_keys:
        raise ValueError("payload 必须是对象且 identity_keys 不得为空")
    identity = tuple(payload.get(key) for key in identity_keys)
    if all(value is None for value in identity):
        raise ValueError("审计记录必须至少包含一个身份字段")
    guard = lock if lock is not None else nullcontext()
    with guard:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(row, dict) and tuple(row.get(key) for key in identity_keys) == identity:
                        return False
        except OSError:
            pass
        was_present = os.path.exists(path)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with open(path, "ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if not was_present:
            _fsync_parent_directory(path)
        return True


def append_jsonl_if_unique(path, payload, identity_keys, *, lock=None):
    """Compatibility name for :func:`append_jsonl_best_effort`.

    The old name does not imply global uniqueness: callers must provide their
    owning lock when duplicate prevention is a correctness invariant.
    """
    return append_jsonl_best_effort(
        path, payload, identity_keys, lock=lock
    )


def atomic_write_jsonl_if_changed(path, rows):
    """Atomically write a rebuildable JSONL artifact only when unchanged.

    This helper is for derived reports/ledgers. Append-only research evidence
    must continue to use its owning state API rather than replacing a log.
    Serialize incrementally so a day-long report does not create one more
    full-size string copy in memory.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    token = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    tmp = f"{path}.tmp.{token}"
    digest = hashlib.sha256()
    try:
        with open(tmp, "wb") as handle:
            for row in (rows or []):
                line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
                handle.write(line)
                digest.update(line)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            same_size = (
                os.path.exists(path)
                and os.path.getsize(path) == os.path.getsize(tmp)
            )
        except OSError:
            same_size = False
        if same_size:
            try:
                existing_digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        existing_digest.update(chunk)
                if existing_digest.digest() == digest.digest():
                    return False
            except OSError:
                pass
        was_present = os.path.exists(path)
        os.replace(tmp, path)
        if not was_present:
            _fsync_parent_directory(path)
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _atomic_replace(path, content):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    token = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    tmp = f"{path}.tmp.{token}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_parent_directory(path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return True


def _fsync_parent_directory(path):
    """Durably publish a successful rename on POSIX; explicit Windows no-op."""
    if os.name != "posix":
        return
    parent = os.path.dirname(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
