"""Small atomic persistence primitives for rebuildable local artifacts."""

import json
import os
import stat
import tempfile


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


def atomic_write_json_if_changed(path, payload, *, ignored_keys=(),
                                 indent=2, ensure_ascii=False,
                                 sort_keys=False, private=False):
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
    return _atomic_replace(path, serialized.encode("utf-8"), private=private)


def _atomic_replace(path, content, *, private=False):
    parent = os.path.dirname(os.path.abspath(path))
    if private and os.name == "posix":
        os.makedirs(parent, mode=0o700, exist_ok=True)
    else:
        os.makedirs(parent, exist_ok=True)
    existing_mode = None
    if private and os.name == "posix":
        try:
            existing_mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                suffix=".tmp", dir=parent)
    try:
        if private and os.name == "posix":
            os.fchmod(fd, existing_mode if existing_mode is not None else 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_parent_directory(path)
    finally:
        if fd is not None:
            os.close(fd)
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
