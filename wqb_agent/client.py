"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Retrieve BRAIN truth and enforce transport/retry/unknown-write rules.
READ WHEN: changing BRAIN requests, Retry-After, or reconciliation.
DO NOT USE FOR: deciding research direction or interpreting hypotheses.

WQB API client with unified retry, classified exceptions and thread safety.

Retry policy (shared by every HTTP call through _request):

- 401                 -> re-authenticate and retry
- 429                 -> honor Retry-After
- 5xx / connection / timeout -> exponential backoff + jitter
- 400 / 403 / 404 / 422 -> fail fast with a classified exception

Thread safety: each thread owns its own requests.Session and auth flag
(thread-local) so concurrent simulations never share a mutable Session; the
first authenticating thread wins through a lock, so simulation workers never
hammer /authentication in parallel.

Credentials (in order): explicit constructor credentials, environment
variables WQB_USERNAME / WQB_PASSWORD, an explicitly configured
WQB_CREDENTIALS_ENV_FILE, or ~/.brain_credentials.txt (username then
password). No cwd, parent-directory, package-directory, or implicit .env
search is performed. The local proxy is bypassed (trust_env=False): a
local proxy rewrites the auth body and causes HTTP 400.

Auth note (verified against the live platform): BRAIN /authentication
accepts HTTP Basic Auth with an *empty* POST body - a JSON body is rejected
with 400 "Unexpected property". Keep the Basic-Auth empty-body form.
"""

import math
import random
import threading
import time
from collections.abc import Mapping

import requests

from .client_transport import backoff_delay, normalize_progress_url
from .credentials import (
    DEFAULT_CREDENTIALS_FILE,
    CredentialError,
    resolve_credentials,
)
from .failures import FailureKind, classify_error
from .protocol import probe_capability_response, retry_after_seconds
from .query_errors import QueryTooBroadError

BASE_URL = "https://api.worldquantbrain.com"

CREDENTIALS_FILE = DEFAULT_CREDENTIALS_FILE

# Status codes that indicate a permanent, non-retryable rejection.
FAIL_FAST_STATUSES = (400, 403, 404, 422)
ALPHA_COLOR_VALUES = frozenset({"BLUE", "GREEN", "PURPLE", "RED", "YELLOW"})


def _finite_nonnegative(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) and parsed >= 0 else float(default)


class WQBError(Exception):
    """Base class for all classified WQB errors."""

    kind = FailureKind.INFRA


class WQBAuthError(WQBError):
    kind = FailureKind.AUTH


class WQBRateLimitError(WQBError):
    kind = FailureKind.RATE_LIMIT


class WQBRejectedError(WQBError):
    kind = FailureKind.SYNTAX


class WQBNotFoundError(WQBError):
    kind = FailureKind.DATA


class WQBTimeoutError(WQBError):
    kind = FailureKind.TIMEOUT


class WQBCorrelationPendingError(WQBTimeoutError):
    """Correlation endpoint was reachable but did not settle in its read budget."""

    kind = FailureKind.TIMEOUT


class WQBSimulationError(WQBError):
    kind = FailureKind.INFRA


class WQBQueryTooBroadError(WQBSimulationError, QueryTooBroadError):
    """The platform requires a narrower date/filter window."""


class WQBSubmitUnknownError(WQBSimulationError):
    """The POST may have reached BRAIN but its outcome is unknowable.

    Retrying this exception would create a second simulation and burn budget.
    Callers must persist it as an unresolved submission instead.
    """


def load_credentials(username_env="WQB_USERNAME", password_env="WQB_PASSWORD"):
    """Compatibility tuple adapter over the deterministic credentials resolver."""
    source = resolve_credentials(
        username_env=username_env,
        password_env=password_env,
        credentials_file=CREDENTIALS_FILE,
    )
    if source is None:
        return None, None
    return source.username, source.password


class WQBClient:
    """Thread-safe WQB API client."""

    def __init__(
        self,
        username=None,
        password=None,
        base_url=BASE_URL,
        instrument_type="EQUITY",
        region="USA",
        delay=1,
        universe="TOP3000",
        max_retries=5,
        submit_spacing_sec=2.0,
    ):
        self.base_url = base_url.rstrip("/")
        explicit_username = username is not None
        explicit_password = password is not None
        if explicit_username != explicit_password:
            raise WQBAuthError(
                "Incomplete explicit WQB credentials; provide both "
                "username and password."
            )
        if explicit_username:
            if not username or not password:
                raise WQBAuthError(
                    "Incomplete explicit WQB credentials; provide both "
                    "username and password."
                )
            self.username, self.password = username, password
        else:
            try:
                self.username, self.password = load_credentials(
                    username_env="WQB_USERNAME",
                    password_env="WQB_PASSWORD",
                )
            except CredentialError as exc:
                raise WQBAuthError(str(exc)) from exc
        if not self.username or not self.password:
            raise WQBAuthError(
                "No credentials found. Set WQB_USERNAME/WQB_PASSWORD "
                "or create ~/.brain_credentials.txt (username, then password)."
            )
        self.instrument_type = instrument_type
        self.region = region
        self.delay = delay
        self.universe = universe
        self.max_retries = max_retries
        self.submit_spacing_sec = max(0.0, float(submit_spacing_sec))
        self._local = threading.local()
        self._auth_lock = threading.Lock()
        # 429 is a client-wide budget, not a per-thread retry counter.  When
        # one worker receives Retry-After, all simulation workers honor the
        # same gate so they do not stampede the platform together.
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_until = 0.0
        self._submit_lock = threading.Lock()
        self._next_submit_at = 0.0

    # ---- thread-local session ----

    def _session(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"Accept": "application/json"})
            # Bypass unstable local proxy: the local proxy rewrites the auth
            # body and causes HTTP 400.
            sess.trust_env = False
            self._local.session = sess
        return sess

    def _is_authenticated(self):
        return bool(getattr(self._local, "authenticated", False))

    def _set_authenticated(self, value):
        self._local.authenticated = value

    def _normalize_progress_url(self, progress_url):
        """Resolve and validate a progress URL before any network request."""
        return normalize_progress_url(
            self.base_url, progress_url, error_type=WQBSimulationError
        )

    # ---- authentication ----

    def _authenticate(self):
        # BRAIN /authentication accepts HTTP Basic Auth with an empty POST
        # body (JSON body is rejected with 400 "Unexpected property").
        transport_attempt = 0
        rate_limit_start = time.monotonic()
        while True:
            self._wait_rate_limit_gate()
            try:
                resp = self._session().post(
                    f"{self.base_url}/authentication",
                    auth=(self.username, self.password),
                    allow_redirects=False,
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                if transport_attempt >= self.max_retries - 1:
                    raise WQBSimulationError(
                        f"Authentication network error after "
                        f"{self.max_retries} attempts: {exc}"
                    ) from exc
                time.sleep(self._backoff(transport_attempt))
                transport_attempt += 1
                continue
            if resp.status_code in (200, 201, 301, 302, 303):
                self._set_authenticated(True)
                return
            if resp.status_code == 401:
                raise WQBAuthError(
                    "Authentication rejected by WorldQuant BRAIN (401)."
                )
            if resp.status_code == 429:
                if time.monotonic() - rate_limit_start >= 1800:
                    raise WQBRateLimitError(
                        "Authentication rate-limit budget exhausted; "
                        "server continued returning 429."
                    )
                self._register_rate_limit(resp)
                continue
            else:
                raise WQBSimulationError(
                    f"Authentication failed with status {resp.status_code}."
                )

    def _ensure_auth(self):
        if self._is_authenticated():
            return
        with self._auth_lock:
            if not self._is_authenticated():
                self._authenticate()

    # ---- retry helpers ----

    @staticmethod
    def _backoff(attempt, base=1.0, cap=30.0):
        return backoff_delay(attempt, base=base, cap=cap)

    def _sleep_retry_after(self, resp):
        """Register and wait for a server-wide Retry-After gate.

        Kept as a compatibility helper for authentication callers. The
        request path uses ``_register_rate_limit`` followed by the shared
        wait at the top of its next iteration, so a 429 never consumes the
        ordinary transport retry budget.
        """
        self._register_rate_limit(resp)
        self._wait_rate_limit_gate()

    @staticmethod
    def _retry_after_seconds(resp):
        return retry_after_seconds(resp)

    def _register_rate_limit(self, resp):
        # A small, bounded jitter prevents all workers from waking on the
        # exact same millisecond, while preserving the complete Retry-After.
        self._ensure_rate_limit_state()
        delay = self._retry_after_seconds(resp)
        jitter = random.uniform(0.0, min(1.0, delay * 0.1))
        until = time.monotonic() + delay + jitter
        with self._rate_limit_lock:
            self._rate_limit_until = max(self._rate_limit_until, until)

    def _wait_rate_limit_gate(self, max_wait=None):
        """Wait for the shared gate, optionally bounded by caller deadline.

        Returning ``False`` means the caller's own deadline was reached before
        the server-wide gate opened; callers must stop rather than issue a
        request after their local budget.
        """
        self._ensure_rate_limit_state()
        while True:
            with self._rate_limit_lock:
                remaining = self._rate_limit_until - time.monotonic()
            if remaining <= 0:
                return True
            if max_wait is not None:
                try:
                    budget = max(0.0, float(max_wait))
                except (TypeError, ValueError):
                    budget = 0.0
                if remaining > budget:
                    if budget > 0:
                        time.sleep(budget)
                    return False
            time.sleep(remaining)

    def _ensure_rate_limit_state(self):
        """Lazy compatibility for clients created via ``__new__`` in tools/tests."""
        if hasattr(self, "_rate_limit_lock"):
            return
        # Attribute assignment is safe enough under the GIL; the temporary
        # duplicate locks can only occur before the object is shared, while
        # normal production construction initializes these in __init__.
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_until = 0.0

    def _wait_submission_slot(self):
        """Stagger simulation POSTs while keeping polling concurrent.

        The first gate check may be separated from the POST by contention on
        the submission-spacing lock.  Recheck after that wait so a concurrent
        429 cannot be missed in the TOCTOU window; ``_request`` also checks
        immediately before the transport call.
        """
        if not hasattr(self, "_submit_lock"):
            self._submit_lock = threading.Lock()
            self._next_submit_at = 0.0
            self.submit_spacing_sec = getattr(self, "submit_spacing_sec", 2.0)
        self._wait_rate_limit_gate()
        with self._submit_lock:
            remaining = self._next_submit_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._wait_rate_limit_gate()
            self._next_submit_at = time.monotonic() + self.submit_spacing_sec

    def _classified_exception(self, status_code, text, context):
        kind = classify_error(text, status_code)
        if status_code:
            msg = f"{context} failed with status {status_code}: {text[:300]}"
        else:
            msg = f"{context}: {text[:300]}"
        if kind == FailureKind.AUTH:
            return WQBAuthError(msg)
        if kind == FailureKind.RATE_LIMIT:
            return WQBRateLimitError(msg)
        if kind == FailureKind.SYNTAX:
            return WQBRejectedError(msg)
        if kind == FailureKind.DATA:
            return WQBNotFoundError(msg)
        if kind == FailureKind.TIMEOUT:
            return WQBTimeoutError(msg)
        return WQBSimulationError(msg)

    # ---- unified request ----

    def _request(self, method, url, *, params=None, json=None, headers=None, timeout=60,
                 accepted=(200,), context="request", rate_limit_budget_sec=1800,
                 ambiguous_write=False, retry_rate_limit=True):
        """Single retry policy shared by every HTTP call.

        429 honors the full Retry-After but only within a total
        rate-limit budget; exhausting the budget raises WQBRateLimitError —
        the request provably never happened (429 rejects it), so callers may
        retry it instead of treating the result as unknown.
        """
        rate_limit_budget_sec = _finite_nonnegative(rate_limit_budget_sec, 1800.0)
        start = time.monotonic()
        transport_attempt = 0
        while True:
            remaining = max(0.0, rate_limit_budget_sec - (time.monotonic() - start))
            if not self._wait_rate_limit_gate(max_wait=remaining):
                raise WQBRateLimitError(
                    f"{context} rate-limit budget exhausted while waiting for shared gate."
                )
            self._ensure_auth()
            # Authentication may itself encounter a 429 and extend the
            # client-wide gate.  Confirm again immediately before transport;
            # Simulation POSTs must not cross that second TOCTOU window.
            remaining = max(0.0, rate_limit_budget_sec - (time.monotonic() - start))
            if not self._wait_rate_limit_gate(max_wait=remaining):
                raise WQBRateLimitError(
                    f"{context} rate-limit budget exhausted before transport."
                )
            try:
                resp = self._session().request(
                    method, url, params=params, json=json, headers=headers, timeout=timeout
                )
            except requests.exceptions.Timeout as exc:
                if ambiguous_write:
                    raise WQBSubmitUnknownError(
                        f"{context} timed out; backend acceptance is unknown."
                    ) from exc
                if transport_attempt >= self.max_retries - 1:
                    raise WQBTimeoutError(
                        f"{context} timed out after {self.max_retries} attempts."
                    ) from exc
                time.sleep(self._backoff(transport_attempt))
                transport_attempt += 1
                continue
            except requests.exceptions.RequestException as exc:
                if ambiguous_write:
                    raise WQBSubmitUnknownError(
                        f"{context} network error; backend acceptance is unknown."
                    ) from exc
                if transport_attempt >= self.max_retries - 1:
                    raise WQBSimulationError(
                        f"{context} network error after {self.max_retries} "
                        f"attempts: {exc}"
                    ) from exc
                time.sleep(self._backoff(transport_attempt))
                transport_attempt += 1
                continue

            if resp.status_code in accepted:
                return resp
            if resp.status_code == 401:
                self._set_authenticated(False)
                self._ensure_auth()
                transport_attempt += 1
                if transport_attempt >= self.max_retries:
                    raise WQBAuthError(f"{context} authentication retries exhausted.")
                continue
            if resp.status_code == 429:
                if not retry_rate_limit:
                    raise WQBRateLimitError(
                        f"{context} received 429; POST acceptance is not contractually known."
                    )
                elapsed = time.monotonic() - start
                remaining = max(0.0, rate_limit_budget_sec - elapsed)
                retry_delay = self._retry_after_seconds(resp)
                if elapsed >= rate_limit_budget_sec or retry_delay > remaining:
                    self._register_rate_limit(resp)
                    raise WQBRateLimitError(
                        f"{context} rate-limit budget exhausted after "
                        f"{int(elapsed)}s; server returned 429 "
                        f"(retry delay {int(retry_delay)}s exceeds remaining budget)."
                    )
                self._register_rate_limit(resp)
                # Deliberately do not increment transport_attempt: 429 means
                # the request was rejected before processing and is governed
                # by the independent wall-clock rate-limit budget.
                continue
            if resp.status_code in FAIL_FAST_STATUSES:
                raise self._classified_exception(
                    resp.status_code, resp.text, context
                )
            if resp.status_code >= 500:
                if ambiguous_write:
                    raise WQBSubmitUnknownError(
                        f"{context} returned HTTP {resp.status_code}; backend acceptance is unknown."
                    )
                if transport_attempt >= self.max_retries - 1:
                    raise self._classified_exception(
                        resp.status_code, resp.text, context
                    )
                time.sleep(self._backoff(transport_attempt))
                transport_attempt += 1
                continue
            transport_attempt += 1
            raise self._classified_exception(resp.status_code, resp.text, context)

    # ---- API ----

    def get_datasets(self):
        resp = self._request(
            "GET",
            f"{self.base_url}/data-sets",
            params={
                "instrumentType": self.instrument_type,
                "region": self.region,
                "delay": self.delay,
                "universe": self.universe,
            },
            context="GET /data-sets",
        )
        return resp.json().get("results", [])

    def get_operator_capability(self):
        """Read the current account's operators through the sole GET path."""
        resp = self._request(
            "GET",
            f"{self.base_url}/operators",
            accepted=(200, 201, 202, 204),
            context="GET /operators",
        )
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            payload = None
        capability = probe_capability_response(
            "operators", resp.status_code, payload
        )
        if not capability.get("valid"):
            # _request accepted only 2xx; preserve a schema failure as an
            # explicit capability result without persisting the raw response.
            return capability
        return capability

    def get_user_alphas(
        self, *, status=None, limit=100, offset=0,
        date_created_after=None, date_created_before=None,
        date_submitted_after=None, date_submitted_before=None,
    ):
        """Read one bounded page of the authenticated user's Alpha library."""
        try:
            page_limit = max(1, min(100, int(limit)))
            page_offset = max(0, int(offset))
        except (TypeError, ValueError) as exc:
            raise ValueError("Alpha page limit/offset 必须是整数") from exc
        params: dict[str, object] = {"limit": page_limit, "offset": page_offset}
        if status is not None:
            if not isinstance(status, str) or not status.strip():
                raise ValueError("Alpha status 必须是非空字符串")
            params["status"] = status.strip().upper()
        date_filters = {
            "dateCreated>": date_created_after,
            "dateCreated<": date_created_before,
            "dateSubmitted>": date_submitted_after,
            "dateSubmitted<": date_submitted_before,
        }
        for key, value in date_filters.items():
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} 的日期过滤值必须是非空字符串")
                params[key] = value.strip()
        resp = self._request(
            "GET",
            f"{self.base_url}/users/self/alphas",
            params=params,
            context="GET /users/self/alphas",
        )
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise WQBSimulationError(
                "GET /users/self/alphas returned an invalid paginated payload."
            )
        return payload

    def get_all_user_alphas(
        self, *, status=None, limit=100, max_pages=1000, max_results=1000,
        date_created_after=None, date_created_before=None,
        date_submitted_after=None, date_submitted_before=None,
    ):
        """Read all pages of the authenticated user's Alpha library.

        The endpoint is paginated and the caller still gets a hard page cap so
        a malformed ``next``/``count`` response cannot create an endless loop.
        """
        try:
            page_limit = max(1, min(100, int(limit)))
            page_cap = max(1, int(max_pages))
            result_cap = max(1, int(max_results))
        except (TypeError, ValueError) as exc:
            raise ValueError("Alpha pagination limit/max_pages 必须是整数") from exc
        rows = []
        seen_ids = set()
        offset = 0
        expected_count = None
        for _ in range(page_cap):
            page = self.get_user_alphas(
                status=status, limit=page_limit, offset=offset,
                date_created_after=date_created_after,
                date_created_before=date_created_before,
                date_submitted_after=date_submitted_after,
                date_submitted_before=date_submitted_before,
            )
            if expected_count is None and isinstance(page.get("count"), int):
                expected_count = max(0, page["count"])
                if expected_count > result_cap:
                    raise WQBQueryTooBroadError(
                        "GET /users/self/alphas query window exceeds the result cap."
                    )
            page_rows = page.get("results") or []
            for row in page_rows:
                if not isinstance(row, dict):
                    continue
                identity = row.get("id")
                if identity is None or str(identity) in seen_ids:
                    continue
                seen_ids.add(str(identity))
                rows.append(row)
            if expected_count is not None and len(rows) >= expected_count:
                return rows
            if not page_rows or page.get("next") is None:
                if expected_count is not None and len(rows) < expected_count:
                    raise WQBSimulationError(
                        "GET /users/self/alphas returned incomplete pagination."
                    )
                return rows
            offset += page_limit
        raise WQBSimulationError(
            "GET /users/self/alphas exceeded the bounded pagination limit."
        )

    def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
        """Fetch datafields of a dataset. ``field_type`` optionally filters by
        BRAIN field type (MATRIX / VECTOR / SCALAR / ...).

        2026-08-19 修复：原实现硬编码 ``type=MATRIX``，导致每个 dataset 的
        VECTOR 字段（news18 实测 50 个，如 composite_sentiment_score_2）从
        未进入发现流程——探索「Vector 字段 + vec_* 算子」方向必须能检索到
        它们。传 ``field_type=None`` 时不加过滤（保持向后兼容：默认与
        旧行为一致）。
        """
        params = {
            "instrumentType": self.instrument_type,
            "region": self.region,
            "delay": self.delay,
            "universe": self.universe,
            "dataset.id": dataset_id,
            "limit": limit,
            "offset": offset,
        }
        if field_type:
            params["type"] = field_type
        resp = self._request(
            "GET",
            f"{self.base_url}/data-fields",
            params=params,
            context=f"GET datafields {dataset_id}",
        )
        payload = resp.json()
        return payload.get("results", []), payload.get("count", 0)

    def submit_simulation(self, expression, settings, alpha_type="REGULAR",
                          idempotency_key=None):
        # MECHANISM_INVARIANT:
        # Ambiguous POST outcomes must reconcile the same remote job; never
        # infer safety from a timeout/429 and issue a duplicate write.
        """Submit once after a durable caller-side ``SUBMITTING`` checkpoint.

        A timeout, transport failure, 5xx, and a 429 without an explicit
        BRAIN write-rejection contract all remain ambiguous to the caller.
        They must become ``SUBMIT_UNKNOWN`` rather than trigger a duplicate
        POST.  The durable caller-side fingerprint supports later read-only
        reconciliation.
        """
        self._wait_submission_slot()
        body = {"type": alpha_type, "settings": settings, "regular": expression}
        headers = {"X-Idempotency-Key": idempotency_key} if idempotency_key else None
        resp = self._request(
            "POST",
            f"{self.base_url}/simulations",
            json=body,
            accepted=(201, 200),
            context=f"submit simulation {expression[:60]}",
            ambiguous_write=True,
            headers=headers,
        )
        location = resp.headers.get("Location")
        if not location:
            raise WQBSubmitUnknownError(
                "Simulation response missing Location header; backend acceptance is unknown."
            )
        try:
            return self._normalize_progress_url(location)
        except WQBError as exc:
            raise WQBSubmitUnknownError(
                "Simulation response contained an invalid progress Location; "
                "backend acceptance is unknown."
            ) from exc

    def submit_multi_simulation(self, simulations, idempotency_key=None):
        """Submit one bounded Multi-Simulation payload.

        The endpoint and write-safety contract are the same as a single
        Simulation; BRAIN receives a JSON array of up to ten regular child
        payloads and returns one parent progress URL.
        """
        if not isinstance(simulations, (list, tuple)) or not simulations:
            raise ValueError("Multi-Simulation requires a non-empty sequence")
        if len(simulations) > 10:
            raise ValueError("Multi-Simulation supports at most 10 children")
        payload = []
        for item in simulations:
            if not isinstance(item, Mapping):
                raise TypeError("Multi-Simulation child must be an object")
            if "regular" in item:
                child = dict(item)
            else:
                child = {
                    "type": "REGULAR",
                    "settings": dict(item.get("settings") or {}),
                    "regular": item.get("expression"),
                }
            if not isinstance(child.get("regular"), str) or not child["regular"].strip():
                raise ValueError("Multi-Simulation child expression must be non-empty")
            child.setdefault("type", "REGULAR")
            payload.append(child)
        headers = {"X-Idempotency-Key": idempotency_key} if idempotency_key else None
        resp = self._request(
            "POST",
            f"{self.base_url}/simulations",
            json=payload,
            accepted=(201, 200),
            context="submit multi simulation",
            ambiguous_write=True,
            headers=headers,
        )
        location = resp.headers.get("Location")
        if not location:
            raise WQBSubmitUnknownError(
                "Multi-Simulation response missing Location header; backend acceptance is unknown."
            )
        try:
            return self._normalize_progress_url(location)
        except WQBError as exc:
            raise WQBSubmitUnknownError(
                "Multi-Simulation response contained an invalid progress Location; "
                "backend acceptance is unknown."
            ) from exc

    def poll_progress(self, progress_url, timeout_sec=1500, progress_callback=None):
        # MECHANISM_INVARIANT:
        # A known progress URL is the only job that may be polled after POST.
        """Poll until the simulation has an alpha id.

        400/403/404/422 fail fast (permanent), 401 re-auths, 429 honors
        Retry-After, 5xx backs off, and the overall deadline is enforced.
        """
        progress_url = self._normalize_progress_url(progress_url)
        timeout_sec = _finite_nonnegative(timeout_sec, 1500.0)
        start = time.monotonic()
        polls = 0
        auth_attempts = 0
        try:
            auth_limit = max(1, int(getattr(self, "max_retries", 3)))
        except (TypeError, ValueError):
            auth_limit = 3
        while True:
            polls += 1
            remaining = max(0.0, timeout_sec - (time.monotonic() - start))
            if remaining <= 0 or not self._wait_rate_limit_gate(max_wait=remaining):
                raise WQBTimeoutError("Simulation polling timed out.")
            self._ensure_auth()
            try:
                resp = self._session().get(
                    progress_url, timeout=max(0.001, min(60.0, remaining))
                )
            except requests.exceptions.RequestException as exc:
                remaining = max(0.0, timeout_sec - (time.monotonic() - start))
                if remaining <= 0:
                    raise WQBTimeoutError(
                        "Simulation polling timed out."
                    ) from exc
                time.sleep(min(self._backoff(0), remaining))
                continue

            if resp.status_code == 401:
                auth_attempts += 1
                if auth_attempts >= auth_limit:
                    raise WQBAuthError(
                        "Simulation polling authentication retries exhausted."
                    )
                self._set_authenticated(False)
                continue
            if resp.status_code == 429:
                remaining = max(0.0, timeout_sec - (time.monotonic() - start))
                retry_delay = self._retry_after_seconds(resp)
                if remaining <= 0 or retry_delay > remaining:
                    raise WQBTimeoutError("Simulation polling timed out.")
                self._register_rate_limit(resp)
                continue
            if resp.status_code in FAIL_FAST_STATUSES:
                raise self._classified_exception(
                    resp.status_code, resp.text, "poll_progress"
                )
            if resp.status_code >= 500:
                remaining = max(0.0, timeout_sec - (time.monotonic() - start))
                if remaining <= 0:
                    raise WQBTimeoutError("Simulation polling timed out.")
                time.sleep(min(self._backoff(0), remaining))
                continue

            retry_after = resp.headers.get("Retry-After")
            if resp.status_code == 200 and retry_after is None:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    raise WQBSimulationError(
                        "Simulation progress returned a non-object JSON payload."
                    )
                alpha_id = payload.get("alpha")
                if alpha_id:
                    return alpha_id
                # 平台返回明确的终止状态（ERROR/FAILED，带真实 sim id 与
                # 错误消息，如语法错误）：这是表达式/设置的确定性拒绝，
                # 不是"结果未知"。按 WQBRejectedError 处理（Simulator 标
                # FAILED、不暂停派发），并保留平台真实 id，避免被误判为
                # UNKNOWN 导致整轮派发暂停。
                status = payload.get("status")
                # BRAIN uses both ``FAIL`` and ``FAILED`` in terminal
                # simulation payloads (the former is common for expression
                # evaluation/runtime rejection).  All are deterministic
                # terminal outcomes, so do not replacement-retry them.
                if status in ("ERROR", "FAIL", "FAILED") or payload.get("message"):
                    sim_id = payload.get("id") or "?"
                    message = payload.get("message") or resp.text[:300]
                    raise WQBRejectedError(
                        f"Simulation rejected by platform: status={status} "
                        f"sim_id={sim_id} message={message}"
                    )
                raise WQBSimulationError(
                    f"Simulation finished without alpha id: {resp.text[:300]}"
                )
            remaining = max(0.0, timeout_sec - (time.monotonic() - start))
            if remaining <= 0:
                raise WQBTimeoutError("Simulation polling timed out.")
            if progress_callback and (polls == 1 or polls % 6 == 0):
                progress_callback(time.monotonic() - start, polls, resp.status_code)
            # Retry-After may be seconds or an HTTP-date; _retry_after_seconds
            # handles both (a bare float() would crash on the date form).
            delay = self._retry_after_seconds(resp)
            time.sleep(min(delay, 30, remaining))

    def poll_multi_progress(self, progress_url, timeout_sec=1500, progress_callback=None):
        """Poll a Multi-Simulation parent and then its child simulations."""
        progress_url = self._normalize_progress_url(progress_url)
        timeout_sec = _finite_nonnegative(timeout_sec, 1500.0)
        start = time.monotonic()
        polls = 0
        while True:
            remaining = max(0.0, timeout_sec - (time.monotonic() - start))
            if remaining <= 0:
                raise WQBTimeoutError("Multi-Simulation polling timed out.")
            snapshot = self.get_progress_snapshot(
                progress_url, timeout=min(60.0, remaining)
            )
            polls += 1
            status_code = snapshot.get("status_code")
            if status_code in FAIL_FAST_STATUSES:
                raise self._classified_exception(
                    status_code, snapshot.get("text", ""), "poll_multi_progress"
                )
            if status_code is not None and status_code >= 500:
                raise WQBSubmitUnknownError(
                    "Multi-Simulation parent returned a server error; outcome is unknown."
                )
            payload = snapshot.get("payload")
            if not isinstance(payload, dict):
                raise WQBSimulationError(
                    "Multi-Simulation progress returned a non-object JSON payload."
                )
            status = payload.get("status")
            if status in ("ERROR", "FAIL", "FAILED") or payload.get("message"):
                raise WQBRejectedError(
                    "Multi-Simulation rejected by platform: "
                    f"status={status} message={payload.get('message', '')}"
                )
            children = payload.get("children")
            if status in ("COMPLETE", "WARNING") or isinstance(payload.get("alphas"), list):
                if isinstance(payload.get("alphas"), list):
                    alpha_ids = [str(item) for item in payload["alphas"] if item]
                    if alpha_ids:
                        return alpha_ids
                if not isinstance(children, list) or not children:
                    raise WQBSimulationError(
                        "Multi-Simulation completed without child simulations."
                    )
                alpha_ids = []
                for child in children:
                    child_id = child.get("id") if isinstance(child, Mapping) else child
                    if not child_id:
                        raise WQBSimulationError(
                            "Multi-Simulation returned a child without an id."
                        )
                    child_url = str(child_id)
                    if not child_url.startswith(("http://", "https://", "/")):
                        child_url = f"{self.base_url}/simulations/{child_url}"
                    alpha_ids.append(self.poll_progress(
                        child_url, timeout_sec=remaining,
                        progress_callback=progress_callback,
                    ))
                return alpha_ids
            if progress_callback:
                progress_callback(
                    time.monotonic() - start, polls, status_code
                )
            retry_after = snapshot.get("retry_after_seconds") or 1.0
            time.sleep(min(float(retry_after), 30.0, remaining))

    def get_progress_snapshot(self, progress_url, timeout=60):
        """Read one known progress URL without creating a second job.

        The reconciliation tool needs the raw terminal/pending status, so it
        cannot use ``poll_progress``.  This public adapter keeps authentication
        and the thread-local session inside the client while returning only a
        transport-neutral snapshot to the caller.
        """
        progress_url = self._normalize_progress_url(progress_url)
        timeout = _finite_nonnegative(timeout, 60.0)
        start = time.monotonic()
        auth_attempts = 0
        try:
            auth_limit = max(1, int(getattr(self, "max_retries", 3)))
        except (TypeError, ValueError):
            auth_limit = 3
        while True:
            remaining = max(0.0, timeout - (time.monotonic() - start))
            if remaining <= 0 or not self._wait_rate_limit_gate(max_wait=remaining):
                raise WQBTimeoutError("Progress snapshot request timed out waiting for rate limit.")
            self._ensure_auth()
            try:
                resp = self._session().get(
                    progress_url, timeout=max(0.001, min(60.0, remaining))
                )
            except requests.exceptions.Timeout as exc:
                raise WQBTimeoutError("Progress snapshot request timed out.") from exc
            except requests.exceptions.RequestException as exc:
                raise WQBSimulationError("Progress snapshot request failed.") from exc
            if resp.status_code != 401:
                break
            auth_attempts += 1
            if auth_attempts >= auth_limit:
                raise WQBAuthError("Progress snapshot authentication retries exhausted.")
            self._set_authenticated(False)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "text": resp.text,
            "payload": payload,
            "retry_after_seconds": self._retry_after_seconds(resp),
        }

    def get_alpha(self, alpha_id):
        resp = self._request(
            "GET", f"{self.base_url}/alphas/{alpha_id}", context=f"GET alpha {alpha_id}"
        )
        return resp.json()

    def get_recordset(self, alpha_id, name):
        """Fetch an explicitly allow-listed read-only Alpha recordset.

        An HTTP-accepted response with an empty body (a freshly simulated
        alpha whose PnL has not settled yet) is a valid "no data" response
        and returns ``None`` instead of raising a JSON parse error that
        would otherwise break the entire evidence snapshot.
        """
        if name not in {"pnl"}:
            raise ValueError(f"unsupported alpha recordset: {name}")
        resp = self._request(
            "GET", f"{self.base_url}/alphas/{alpha_id}/recordsets/{name}",
            context=f"GET alpha recordset {name} {alpha_id}",
        )
        try:
            return resp.json()
        except ValueError:
            return None

    def get_pnl(self, alpha_id):
        """Fetch the official daily PnL recordset without inventing metrics."""
        return self.get_recordset(alpha_id, "pnl")

    def set_alpha_color(self, alpha_id, color, *, verify=True):
        """Update only top-level Alpha metadata color and optionally read it back.

        This deliberately uses the metadata PATCH endpoint.  It never calls an
        Alpha submit endpoint and therefore cannot create a new Simulation.
        ``None`` clears a project-owned color when the live API supports it.
        """
        if not isinstance(alpha_id, (str, int)) or not str(alpha_id).strip():
            raise ValueError("alpha_id must be a non-empty string or integer")
        normalized = None if color is None else str(color).strip().upper()
        if normalized is not None and normalized not in ALPHA_COLOR_VALUES:
            raise ValueError(
                f"unsupported Alpha color {color!r}; expected one of "
                f"{sorted(ALPHA_COLOR_VALUES)} or None"
            )
        alpha_id = str(alpha_id).strip()
        resp = self._request(
            "PATCH",
            f"{self.base_url}/alphas/{alpha_id}",
            json={"color": normalized},
            accepted=(200, 201, 204),
            context=f"PATCH alpha color {alpha_id}",
        )
        if not verify:
            try:
                return resp.json()
            except ValueError:
                return {"id": alpha_id, "color": normalized}
        payload = self.get_alpha(alpha_id)
        actual = payload.get("color") if isinstance(payload, dict) else None
        if actual != normalized:
            raise WQBError(
                f"Alpha {alpha_id} color readback mismatch: "
                f"{actual!r} != {normalized!r}"
            )
        return payload

    def get_aggregates(self, alpha_id):
        """Fetch per-year aggregate metrics for an alpha (BRAIN /alphas/{id}/aggregates).

        Returns the raw payload (typically {'is': {'yearlyData': [...]}}) so the
        caller can inspect year-by-year sharpe/returns/turnover stability.
        """
        resp = self._request(
            "GET",
            f"{self.base_url}/alphas/{alpha_id}/aggregates",
            context=f"GET alpha aggregates {alpha_id}",
        )
        return resp.json()

    def get_correlation(self, alpha_id, kind="self", timeout_sec=300):
        """Poll a read-only alpha-correlation endpoint until it settles."""
        if kind not in ("self", "prod"):
            raise ValueError("kind must be 'self' or 'prod'")
        budget = _finite_nonnegative(timeout_sec, 300.0)
        start = time.monotonic()
        while True:
            resp = self._request(
                "GET",
                f"{self.base_url}/alphas/{alpha_id}/correlations/{kind}",
                timeout=60,
                rate_limit_budget_sec=budget,
                context=f"GET {kind} correlation {alpha_id}",
            )
            retry_after = resp.headers.get("Retry-After")
            if retry_after is None:
                return resp.json()
            remaining = max(0.0, budget - (time.monotonic() - start))
            if remaining <= 0:
                raise WQBCorrelationPendingError(
                    f"{kind} correlation timed out after {timeout_sec}s"
                )
            time.sleep(min(self._retry_after_seconds(resp), 30, remaining))

    def get_self_correlation(self, alpha_id, timeout_sec=20):
        """Fetch the settled SELF_CORRELATION payload through the client API."""
        return self.get_correlation(alpha_id, kind="self", timeout_sec=timeout_sec)
