"""Pure transport policy helpers; no Session, auth, or endpoint ownership."""

import random
from urllib.parse import urljoin, urlparse, urlunsplit


def normalize_progress_url(base_url, progress_url, error_type=ValueError):
    if not isinstance(progress_url, str) or not progress_url.strip():
        raise error_type("Progress URL is missing or malformed.")
    base = urlparse(base_url)
    resolved = urlparse(urljoin(base_url.rstrip("/") + "/", progress_url.strip()))
    try:
        base_port, resolved_port = base.port, resolved.port
    except ValueError as exc:
        raise error_type("Progress URL has an invalid port.") from exc
    base_effective_port = base_port or (443 if base.scheme == "https" else 80)
    resolved_effective_port = resolved_port or (443 if resolved.scheme == "https" else 80)
    if (
        not base.scheme or not base.hostname or base.username or base.password
        or not resolved.scheme or not resolved.hostname
        or resolved.username or resolved.password or resolved.fragment
        or (resolved.scheme, resolved.hostname.lower(), resolved_effective_port)
        != (base.scheme, base.hostname.lower(), base_effective_port)
    ):
        raise error_type(
            "Progress URL must be same-origin and contain no credentials or fragment."
        )
    return urlunsplit((resolved.scheme, resolved.netloc, resolved.path or "/",
                       resolved.query, ""))


def backoff_delay(attempt, *, base=1.0, cap=30.0, random_value=None):
    jitter = random.uniform(0.0, base) if random_value is None else float(random_value)
    return min(cap, base * (2 ** attempt) + jitter)
