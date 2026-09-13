"""Process-lifetime owner lock shared by production and maintenance tools."""

import hashlib
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .artifacts import atomic_write_json_if_changed

_LOCK_HANDLES = {}
_PATH_GUARDS = {}
_OWNER_STATES = {}
_REGISTRY_LOCK = threading.Lock()


class OwnerBusyError(RuntimeError):
    """The state-dir owner is held by another process or thread."""

    def __init__(self, lock_path):
        self.lock_path = lock_path
        super().__init__(f"state owner busy: {lock_path}")


@dataclass
class _OwnerState:
    thread_id: int
    depth: int
    os_handle: object
    metadata: bool


@dataclass
class _OwnerToken:
    lock_path: str
    thread_id: int
    metadata: bool
    released: bool = False


def _normalized_lock_path(state_dir):
    return os.path.abspath(os.path.join(state_dir, "run.lock"))


def _path_guard(lock_path):
    with _REGISTRY_LOCK:
        return _PATH_GUARDS.setdefault(lock_path, threading.RLock())


def state_owner_held(state_dir):
    """Return whether this process currently holds the state-dir owner."""
    lock_path = _normalized_lock_path(state_dir)
    with _REGISTRY_LOCK:
        return lock_path in _OWNER_STATES


def _write_owner_metadata(lock_path, operation):
    atomic_write_json_if_changed(lock_path, {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "operation": operation,
    })


def _acquire_owner(state_dir, operation, *, metadata):
    os.makedirs(state_dir, exist_ok=True)
    lock_path = _normalized_lock_path(state_dir)
    guard = _path_guard(lock_path)
    thread_id = threading.get_ident()
    if not guard.acquire(blocking=False):
        raise OwnerBusyError(lock_path)
    state = _OWNER_STATES.get(lock_path)
    if state is not None:
        if state.thread_id != thread_id:
            guard.release()
            raise OwnerBusyError(lock_path)
        state.depth += 1
        return _OwnerToken(lock_path, thread_id, metadata)
    handle = _acquire_os_lock(lock_path)
    if handle is None:
        guard.release()
        raise OwnerBusyError(lock_path)
    try:
        if metadata:
            _write_owner_metadata(lock_path, operation)
        _OWNER_STATES[lock_path] = _OwnerState(
            thread_id=thread_id, depth=1, os_handle=handle, metadata=metadata
        )
    except Exception:
        _release_os_lock(handle)
        guard.release()
        raise
    return _OwnerToken(lock_path, thread_id, metadata)


def _release_owner(token):
    if token is None or token.released:
        return
    if token.thread_id != threading.get_ident():
        raise RuntimeError("state owner must be released by its owning thread")
    lock_path = token.lock_path
    state = _OWNER_STATES.get(lock_path)
    if state is None or state.thread_id != token.thread_id:
        raise RuntimeError("state owner is not held by the current thread")
    token.released = True
    state.depth -= 1
    guard = _path_guard(lock_path)
    try:
        if state.depth == 0:
            _OWNER_STATES.pop(lock_path, None)
            if state.metadata:
                try:
                    with open(lock_path, encoding="utf-8") as handle_file:
                        data = json.load(handle_file)
                    if int(data.get("pid") or 0) == os.getpid():
                        os.remove(lock_path)
                except Exception:
                    pass
            _release_os_lock(state.os_handle)
    finally:
        guard.release()


@contextmanager
def single_instance_scope(state_dir, operation="run-proposals"):
    """Own one state directory for the duration of a mutation transaction."""
    token = _acquire_owner(state_dir, operation, metadata=True)
    try:
        yield token
    finally:
        _release_owner(token)


def acquire_single_instance_lock(state_dir, operation="run-proposals"):
    """Acquire the OS owner lock and write human-readable metadata."""
    lock_path = _normalized_lock_path(state_dir)
    returned_path = os.path.join(state_dir, "run.lock")
    try:
        token = _acquire_owner(state_dir, operation, metadata=True)
    except OwnerBusyError:
        old_pid, started = "?", "?"
        try:
            with open(lock_path, encoding="utf-8-sig") as handle_file:
                data = json.load(handle_file)
            old_pid = data.get("pid", "?")
            started = data.get("started_at", "?")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        print(
            f"[LOCK] 已有研究实例 PID={old_pid} 在运行（启动于 {started}）。\n"
            "       单实例纪律：请等待其完成，不要并行启动第二个实例。"
        )
        return None
    _LOCK_HANDLES.setdefault(lock_path, []).append(token)
    return returned_path


def _acquire_os_lock(lock_path):
    """Acquire a process-lifetime OS lock; stale metadata is not ownership."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        digest = hashlib.sha256(
            os.path.abspath(lock_path).lower().encode("utf-8")
        ).hexdigest()
        handle = create_mutex(None, True, f"Local\\WQBAlpha_{digest}")
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return ("win", handle)
    import fcntl

    guard_path = lock_path + ".guard"
    fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return ("posix", fd)


def _release_os_lock(handle):
    kind, value = handle
    if kind == "win":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex(value)
        kernel32.CloseHandle(value)
        return
    import fcntl

    fcntl.flock(value, fcntl.LOCK_UN)
    os.close(value)


def acquire_os_owner_lock(lock_path):
    """Acquire only the OS owner primitive for maintenance tools.

    Unlike ``acquire_single_instance_lock`` this does not rewrite the
    human-readable ``run.lock`` metadata.  It is the narrow public boundary
    for scripts that must coordinate with the production owner.
    """
    state_dir = os.path.dirname(os.path.abspath(lock_path))
    return _acquire_owner(state_dir, "maintenance", metadata=False)


def release_os_owner_lock(handle):
    """Release a handle returned by :func:`acquire_os_owner_lock`."""
    if handle is not None:
        _release_owner(handle)


def release_single_instance_lock(lock_path):
    if not lock_path:
        return
    normalized_path = os.path.abspath(lock_path)
    tokens = _LOCK_HANDLES.get(normalized_path) or []
    if tokens:
        token = tokens.pop()
        if not tokens:
            _LOCK_HANDLES.pop(normalized_path, None)
        _release_owner(token)
