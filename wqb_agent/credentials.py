"""Deterministic, local-only credential source resolution."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field

DEFAULT_CREDENTIALS_FILE = os.path.expanduser("~/.brain_credentials.txt")
EXPLICIT_ENV_FILE_VARIABLE = "WQB_CREDENTIALS_ENV_FILE"


class CredentialError(ValueError):
    """A configured credential source is malformed or incomplete."""


@dataclass(frozen=True, repr=False)
class CredentialSource:
    """A selected credential pair whose repr never exposes secret values."""

    username: str = field(repr=False)
    password: str = field(repr=False)
    source: str

    def __repr__(self):
        return f"CredentialSource(source={self.source!r})"


def _present(value):
    """Treat only None and the empty string as absent."""
    return value is not None and value != ""


def _complete_pair(username, password, *, source):
    has_username = _present(username)
    has_password = _present(password)
    if has_username and has_password:
        return username, password
    if not has_username and not has_password:
        return None
    raise CredentialError(f"Incomplete {source}")


def _read_text_lines(path, *, source):
    if os.name == "posix" and os.path.islink(path):
        raise CredentialError(f"{source} must not be a symbolic link")
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                file_info = os.fstat(descriptor)
                if not stat.S_ISREG(file_info.st_mode):
                    raise CredentialError(f"{source} must be a regular file")
                if file_info.st_mode & 0o077:
                    raise CredentialError(
                        f"{source} permissions are unsafe; credential file permissions must be private"
                    )
                with os.fdopen(descriptor, encoding="utf-8") as stream:
                    descriptor = None
                    return [line.strip() for line in stream if line.strip()]
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        with open(path, encoding="utf-8") as stream:
            return [line.strip() for line in stream if line.strip()]
    except CredentialError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CredentialError(f"{source} exists but is unreadable") from exc


def _read_credentials_file(path):
    lines = _read_text_lines(path, source="Credential file")
    if len(lines) < 2:
        raise CredentialError(
            "Credential file exists but does not contain username/password"
        )
    return lines[0], lines[1]


def _parse_env_file(path, *, username_env, password_env):
    values = {}
    try:
        lines = _read_text_lines(path, source="Explicit credential environment file")
    except CredentialError as exc:
        if any(token in str(exc) for token in ("permissions are unsafe", "symbolic link", "regular file")):
            raise
        raise CredentialError("Explicit credential environment file is unreadable") from exc
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")

    username = values.get(username_env) or values.get("BRAIN_USERNAME")
    password = values.get(password_env) or values.get("BRAIN_PASSWORD")
    pair = _complete_pair(
        username,
        password,
        source="credentials in explicit environment file",
    )
    if pair is None:
        raise CredentialError(
            "Explicit credential environment file does not contain "
            "username/password"
        )
    return pair


def resolve_credentials(
    *,
    username_env="WQB_USERNAME",
    password_env="WQB_PASSWORD",
    credentials_file=None,
    env_file_path=None,
):
    """Select one complete credential source without directory searching.

    The system environment pair has priority over explicitly configured files.
    ``env_file_path`` is intentionally explicit; when omitted, the opt-in
    ``WQB_CREDENTIALS_ENV_FILE`` variable is consulted.  A configured source
    that is incomplete, malformed, or unreadable raises instead of falling
    through to another source.
    """
    pair = _complete_pair(
        os.environ.get(username_env),
        os.environ.get(password_env),
        source="WQB credentials in environment",
    )
    if pair is not None:
        return CredentialSource(
            username=pair[0], password=pair[1], source="environment"
        )

    selected_env_file = (
        env_file_path
        if env_file_path is not None
        else os.environ.get(EXPLICIT_ENV_FILE_VARIABLE)
    )
    if selected_env_file:
        if not os.path.isabs(selected_env_file):
            raise CredentialError(
                "Explicit credential environment file path must be absolute"
            )
        pair = _parse_env_file(
            selected_env_file,
            username_env=username_env,
            password_env=password_env,
        )
        return CredentialSource(
            username=pair[0],
            password=pair[1],
            source="explicit environment file",
        )

    selected_credentials_file = (
        DEFAULT_CREDENTIALS_FILE
        if credentials_file is None
        else credentials_file
    )
    if os.path.exists(selected_credentials_file):
        pair = _read_credentials_file(selected_credentials_file)
        return CredentialSource(
            username=pair[0], password=pair[1], source="home credentials file"
        )
    return None
