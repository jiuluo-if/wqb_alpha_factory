"""Optional read-only and explicitly enabled Research MCP transports.

This module owns transport, bounded projection, and error presentation only.
Research and platform semantics remain in :mod:`wqb_agent.research_api`.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping
from typing import Any

from . import research_api

MAX_RESULT_BYTES = 48 * 1024
MAX_LIST_ITEMS = 20
MAX_STRING_CHARS = 8 * 1024
MAX_RECORDSETS = 2
MAX_SIMULATION_BATCH = 50
MAX_MULTI_BATCH = research_api.MULTI_MAX_CANDIDATES_PER_CALL
RESEARCH_WRITE_OPT_IN = "ALPHA_FACTORY_ENABLE_SIMULATION_WRITES"
_SECRET_PARTS = ("password", "credential", "token", "secret", "authorization", "cookie", "api_key", "apikey")
_SPEC_KEYS = frozenset({"expression", "settings", "fields", "field_datasets", "proposal_id", "note", "template_id", "simulation_type"})
_WRITE_RESULT_KEYS = (
    "proposal_id", "status", "reason_code", "fingerprint", "alpha_id",
    "field_validation",
)
_TEMPLATE_INVENTORY_KEYS = (
    "template_id", "version", "kind", "family", "role", "economic",
    "economic_mechanism", "direction_reason",
    "required_slots", "economic_field_slots", "control_slots",
    "economic_field_count", "field_roles", "allowed_field_families",
    "field_relationship", "relationship_contract", "semantic_contract",
    "direction", "direction_transform", "expected_horizon", "falsification",
    "self_correlation_impact", "numeric_slots", "allowed_horizon_profiles",
    "allowed_settings_arms", "mechanism_tags", "novelty_family",
    "template_mode", "operator_slots", "tags", "source",
)


def _is_secret_key(key: object) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return any("".join(char for char in part if char.isalnum()) in normalized for part in _SECRET_PARTS)


def _bounded(
    value: Any, *, allow_expression: bool = False,
    list_item_limit: int = MAX_LIST_ITEMS,
) -> tuple[Any, bool]:
    """Return JSON-safe data with bounded depth, lists, strings, and secrets redacted."""
    truncated = False

    def project(item: Any, depth: int) -> Any:
        nonlocal truncated
        if depth > 8:
            truncated = True
            return "[TRUNCATED:MAX_DEPTH]"
        if isinstance(item, Mapping):
            result = {}
            rows = list(item.items())
            if len(rows) > list_item_limit:
                rows = rows[:list_item_limit]
                truncated = True
            for key, child in rows:
                name = str(key)
                if _is_secret_key(name):
                    result[name] = "[REDACTED]"
                elif name.lower() in {"expression", "alpha_expression"} and not allow_expression:
                    result[name] = "[REDACTED]"
                else:
                    result[name] = project(child, depth + 1)
            return result
        if isinstance(item, (list, tuple, set)):
            values = list(item)
            if len(values) > list_item_limit:
                values = values[:list_item_limit]
                truncated = True
            return [project(child, depth + 1) for child in values]
        if isinstance(item, str):
            if len(item) > MAX_STRING_CHARS:
                truncated = True
                return item[:MAX_STRING_CHARS] + "[TRUNCATED]"
            return item
        if isinstance(item, float) and not math.isfinite(item):
            return "NON_FINITE"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:MAX_STRING_CHARS]

    result = project(value, 0)
    return result, truncated


def _error_code(exc: Exception) -> str:
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    if status == 401:
        return "AUTHENTICATION_REQUIRED"
    if status == 403:
        return "PERMISSION_DENIED"
    if status == 429:
        return "RATE_LIMITED"
    if status == 404:
        return "EVIDENCE_NOT_FOUND"
    return "REMOTE_READ_FAILED"


def _short_text(value: Any, *, limit: int = 128) -> str:
    if isinstance(value, str):
        return value[:limit]
    return str(value)[:limit]


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _envelope(
    payload: Mapping[str, Any], *, owner: str, allow_expression: bool = False,
    list_item_limit: int = MAX_LIST_ITEMS,
) -> dict[str, Any]:
    source = _short_text(payload.get("source") or "UNKNOWN")
    bounded, truncated = _bounded(
        payload, allow_expression=allow_expression,
        list_item_limit=list_item_limit,
    )
    raw_status = payload.get("status")
    if isinstance(raw_status, Mapping):
        status = _short_text(raw_status.get("alpha_detail") or "UNKNOWN")
    elif raw_status is not None:
        status = _short_text(raw_status)
    elif isinstance(payload.get("alpha"), Mapping):
        status = "AVAILABLE"
    elif isinstance(payload.get("fields"), list):
        status = "AVAILABLE" if payload["fields"] else "UNAVAILABLE"
    else:
        status = "UNKNOWN"
    evidence_status = _short_text(payload.get("evidence_status") or (
        "AVAILABLE" if status == "AVAILABLE" else
        "UNAVAILABLE" if status == "UNAVAILABLE" else "UNKNOWN"
    ))
    fetched_at = payload.get("fetched_at")
    if isinstance(fetched_at, str):
        fetched_at = fetched_at[:128]
    elif not _finite_number(fetched_at):
        fetched_at = None
    age_sec = payload.get("age_sec")
    if not _finite_number(age_sec):
        age_sec = None
    envelope = {
        "access_mode": "READ_ONLY",
        "owner": owner,
        "source": source,
        "status": status,
        "evidence_status": evidence_status,
        "freshness": (
            "READ_AT_CALL" if source in {"LIVE", "BRAIN_LIVE", "BRAIN_LIVE_ONLY"}
            else "LOCAL_STATE_AT_CALL" if source == "LOCAL_EXECUTION_GUARD"
            else "OWNER_DECLARED"
        ),
        "fetched_at": fetched_at,
        "age_sec": age_sec,
        "truncated": truncated,
        "data": bounded,
    }
    try:
        size = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        size = MAX_RESULT_BYTES + 1
    if size > MAX_RESULT_BYTES:
        envelope["data"] = None
        envelope["truncated"] = True
        envelope["truncation_reason"] = "MAX_RESULT_BYTES"
        envelope["result_bytes_before_truncation"] = size
    return envelope


def _failed_result(exc: Exception, *, owner: str, source: str = "BRAIN_LIVE") -> dict[str, Any]:
    return {
        "access_mode": "READ_ONLY",
        "owner": owner,
        "source": source,
        "status": "UNAVAILABLE",
        "evidence_status": "UNAVAILABLE",
        "freshness": "NOT_READ",
        "fetched_at": None,
        "age_sec": None,
        "truncated": False,
        "data": None,
        "error": _error_code(exc),
    }


def _invalid_result(
    *, owner: str, source: str = "NOT_READ", access_mode: str = "READ_ONLY",
    remote_write: bool = False, failure_code: str | None = None,
) -> dict[str, Any]:
    result = {
        "access_mode": access_mode,
        "owner": owner,
        "source": source,
        "status": "INVALID_ARGUMENT",
        "evidence_status": "NOT_APPLICABLE",
        "freshness": "NOT_READ",
        "fetched_at": None,
        "age_sec": None,
        "truncated": False,
        "data": None,
        "error": failure_code or "INVALID_ARGUMENT",
        **({"remote_write": True} if remote_write else {}),
    }
    if failure_code:
        result["status"] = "NOT_DISPATCHED"
        result["reason_code"] = failure_code
    return result


def _pending_guard_payload(api, *, state_dir, config):
    result = api.get_pending_executions(state_dir=state_dir, config=config)
    if not isinstance(result, Mapping) or not isinstance(result.get("entries"), list):
        raise ValueError("EXECUTION_STATE_UNAVAILABLE")
    return {
        "source": "LOCAL_EXECUTION_GUARD",
        "status": "AVAILABLE",
        "evidence_status": "AVAILABLE",
        "fetched_at": None,
        "entries": result["entries"],
    }


def _private_catalog_failure(
    exc: Exception, *, owner: str, source: str = "LOCAL_PRIVATE_CATALOG",
) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if code == "PRIVATE_TEMPLATE_CATALOG_MISSING":
        return {
            "access_mode": "READ_ONLY",
            "owner": owner,
            "source": "LOCAL_PRIVATE_CATALOG",
            "status": "UNAVAILABLE",
            "evidence_status": "UNAVAILABLE",
            "freshness": "NOT_READ",
            "fetched_at": None,
            "age_sec": None,
            "truncated": False,
            "data": None,
            "error": code,
        }
    return _failed_result(exc, owner=owner, source=source)


def _candidate_spec_payload(spec: Any) -> dict[str, Any] | None:
    if isinstance(spec, Mapping):
        raw = dict(spec)
    else:
        names = (
            "expression", "settings", "fields", "field_datasets", "proposal_id",
            "note", "template_id", "simulation_type",
        )
        if any(not hasattr(spec, name) for name in names):
            return None
        raw = {name: getattr(spec, name) for name in names}
    if set(raw) - _SPEC_KEYS or not isinstance(raw.get("expression"), str):
        return None
    raw["settings"] = dict(raw.get("settings") or {})
    raw["fields"] = list(raw.get("fields") or ())
    raw["field_datasets"] = dict(raw.get("field_datasets") or {})
    raw.setdefault("proposal_id", None)
    raw.setdefault("note", None)
    raw.setdefault("template_id", None)
    raw.setdefault("simulation_type", "REGULAR")
    return raw


def build_server(*, api=research_api, client=None, config=None, state_dir=None):
    """Build an MCP server containing only bounded READ_ONLY facade tools."""
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "MCP support is optional; install alpha-factory[mcp] to run this server."
        ) from exc

    server = MCPServer(
        "alpha-factory-readonly",
        instructions=(
            "All tools are READ_ONLY. This server is a transport adapter over "
            "wqb_agent.research_api; it never submits simulations or Alphas."
        ),
    )
    remote_read = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    local_read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    active_client = client

    def resolve_client():
        nonlocal active_client
        if active_client is None:
            from .client import WQBClient

            active_client = WQBClient()
        return active_client

    def wrap(call, *, owner: str, allow_expression: bool = False, failure_source: str = "BRAIN_LIVE"):
        try:
            payload = call()
        except Exception as exc:  # Keep remote messages/URLs/expressions out of MCP errors.
            return _failed_result(exc, owner=owner, source=failure_source)
        if not isinstance(payload, Mapping):
            payload = {
                "source": "UNKNOWN", "status": "UNKNOWN",
                "evidence_status": "UNAVAILABLE", "result": payload,
            }
        return _envelope(payload, owner=owner, allow_expression=allow_expression)

    @server.tool(annotations=remote_read)
    def get_capabilities() -> dict[str, Any]:
        """[READ_ONLY] Read current BRAIN operator capability through research_api."""
        return wrap(
            lambda: api.get_capabilities(client=resolve_client(), config=config),
            owner="research_api.get_capabilities",
        )

    @server.tool(annotations=remote_read)
    def get_simulation_modes() -> dict[str, Any]:
        """[READ_ONLY] Read account authentication and advertised Simulation permissions; unknown stays unknown."""
        def read_modes():
            modes = api.get_simulation_modes(client=resolve_client(), config=config)
            live = any(
                item.get("source") == "BRAIN_LIVE"
                for item in modes.values()
                if isinstance(item, Mapping)
            )
            return {
                "source": "BRAIN_LIVE" if live else "UNKNOWN",
                "status": "AVAILABLE" if live else "UNKNOWN",
                "evidence_status": "AVAILABLE" if live else "UNAVAILABLE",
                "modes": modes,
            }

        return wrap(
            read_modes,
            owner="research_api.get_simulation_modes",
        )

    @server.tool(annotations=remote_read)
    def list_datafields(dataset_id: str, limit: int = 20, offset: int = 0, field_type: str | None = None) -> dict[str, Any]:
        """[READ_ONLY] Read one live data-field page; output is capped at 20 rows."""
        if (
            not dataset_id.strip() or len(dataset_id) > 256
            or (field_type is not None and len(field_type) > 64)
            or not 1 <= limit <= 50 or offset < 0
        ):
            return _invalid_result(owner="research_api.list_datafields")
        return wrap(
            lambda: api.list_datafields(
                dataset_id, client=resolve_client(), config=config,
                limit=limit, offset=offset, field_type=field_type,
            ),
            owner="research_api.list_datafields",
        )

    @server.tool(annotations=remote_read)
    def get_alpha_evidence(
        alpha_id: str, recordsets: list[str] | None = None,
        depth: str = "summary",
    ) -> dict[str, Any]:
        """[READ_ONLY] Read one live Alpha evidence snapshot.

        The default summary stays cheap. Request ``depth='full'`` for finalist
        evidence including PnL and self-correlation; explicitly requested
        recordsets also require full depth.
        """
        selected = recordsets or []
        if (
            not alpha_id.strip() or len(alpha_id) > 128
            or len(selected) > MAX_RECORDSETS
            or depth not in {"summary", "full"}
            or any(
                not isinstance(name, str) or not name.strip() or len(name) > 128
                for name in selected
            )
        ):
            return _invalid_result(owner="research_api.get_alpha_evidence")
        return wrap(
            lambda: api.get_alpha_evidence(
                alpha_id, client=resolve_client(), config=config, live=True,
                recordsets=selected,
                depth="full" if selected else depth,
            ),
            owner="research_api.get_alpha_evidence",
            allow_expression=True,
        )

    @server.tool(annotations=local_read)
    def get_pending_executions() -> dict[str, Any]:
        """[READ_ONLY] Read bounded unresolved ExecutionGuard summaries from local state; never resumes or edits them."""
        return wrap(
            lambda: _pending_guard_payload(api, state_dir=state_dir, config=config),
            owner="research_api.get_pending_executions",
            failure_source="LOCAL_EXECUTION_GUARD",
        )

    return server


def _write_result_envelope(results, *, expected_count: int) -> dict[str, Any]:
    """Return required proposal attribution and follow-up identities only."""
    malformed = not isinstance(results, (list, tuple))
    results = [] if malformed else list(results)
    projected: list[dict[str, Any]] = []
    scan: dict[str, Any] | None = None
    clipped = False
    for item in results:
        if not isinstance(item, Mapping):
            clipped = True
            projected.append({"status": "UNKNOWN"})
            continue
        row = {key: item[key] for key in _WRITE_RESULT_KEYS if isinstance(item.get(key), str)}
        row.setdefault("status", "UNKNOWN")
        clipped |= (
            not row.get("proposal_id") or not row.get("fingerprint")
            or not isinstance(item.get("status"), str)
            or not row.get("field_validation")
            or item.get("alpha_id") is not None and not isinstance(item.get("alpha_id"), str)
        )
        projected.append(row)
        duplicate_scan = item.get("remote_duplicate_scan")
        if scan is None and isinstance(duplicate_scan, Mapping):
            scan = {}
            for key in ("status", "lookback_days", "complete", "elapsed_sec", "rows_scanned", "matched_count", "candidate_count"):
                value = duplicate_scan.get(key)
                if isinstance(value, str):
                    clipped |= len(value) > 32
                    scan[key] = value[:32]
                elif isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value):
                    scan[key] = value
    projected.extend({"status": "UNKNOWN"} for _ in range(max(0, expected_count - len(projected))))
    envelope = {
        "access_mode": "SIMULATION_WRITE",
        "remote_write": True,
        "truncated": malformed or clipped or len(projected) != len(results) or len(results) != expected_count,
        "candidate_count": expected_count,
        "result_count": len(projected),
        "results": projected,
    }
    if scan is not None:
        envelope["remote_duplicate_scan"] = scan
    return envelope


def build_research_server(*, api=research_api, client=None, config=None, state_dir=None):
    """Build the separately authorized MCP transport for live research writes."""
    if os.environ.get(RESEARCH_WRITE_OPT_IN) != "1":
        raise RuntimeError("RESEARCH_WRITE_MODE_NOT_ENABLED")
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "MCP support is optional; install alpha-factory[mcp] to run this server."
        ) from exc

    server = MCPServer(
        "alpha-factory-research",
        instructions=(
            "Explicitly authorized Research Mode. Simulation tools perform remote BRAIN writes "
            "only through wqb_agent.research_api and SimulationGateway. Alpha submission is unavailable."
        ),
    )
    remote_read = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    local_read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    simulation_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    active_client = client

    def resolve_client():
        nonlocal active_client
        if active_client is None:
            from .client import WQBClient
            active_client = WQBClient()
        return active_client

    def read_facade(name, *args, allow_expression=False, **kwargs):
        try:
            payload = getattr(api, name)(
                *args, client=resolve_client(), config=config, **kwargs,
            )
        except Exception as exc:  # Do not return remote messages, URLs, or secrets.
            return _failed_result(exc, owner=f"research_api.{name}")
        if not isinstance(payload, Mapping):
            payload = {"source": "UNKNOWN", "status": "UNKNOWN", "result": payload}
        return _envelope(payload, owner=f"research_api.{name}", allow_expression=allow_expression)

    def write_facade(name, specs, **options):
        count = len(specs)
        try:
            results = getattr(api, name)(
                specs, client=resolve_client(), config=config, state_dir=state_dir,
                **options,
            )
        except Exception as exc:  # Never serialize exception text from a write path.
            code = getattr(exc, "reason_code", None)
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or not code.isupper():
                code = "REMOTE_WRITE_FAILED"
            result = _write_result_envelope([{"status": "UNAVAILABLE"}] * count, expected_count=count)
            result.update(status="UNAVAILABLE", reason_code=code[:64], error="REMOTE_WRITE_FAILED")
            return result
        return _write_result_envelope(results, expected_count=count)

    def parse_specs(specs, *, minimum, maximum, require_proposal_ids=False):
        if not isinstance(specs, list) or not minimum <= len(specs) <= maximum:
            return None
        if any(not isinstance(spec, Mapping) or set(spec) - _SPEC_KEYS for spec in specs):
            return None
        if require_proposal_ids:
            proposal_ids = [spec.get("proposal_id") for spec in specs]
            if any(
                not isinstance(value, str) or not value.strip() or len(value.strip()) > 48
                for value in proposal_ids
            ):
                return None
            normalized_ids = [value.strip() for value in proposal_ids]
            if len(set(normalized_ids)) != len(normalized_ids):
                return None
        try:
            return [research_api.SimulationSpec(**spec) for spec in specs]
        except (TypeError, ValueError):
            return None

    @server.tool(annotations=remote_read)
    def research_status() -> dict[str, Any]:
        """[READ_ONLY] First call: live readiness and unresolved remote state."""
        return read_facade("research_status", state_dir=state_dir)

    @server.tool(annotations=remote_read)
    def research_batch_status(specs: list[dict[str, Any]]) -> dict[str, Any]:
        """[READ_ONLY] Check each candidate and the Gateway's current Multi window.

        The result includes READY proposal counts, available parent slots,
        children per parent, and the upper-bound children in the active window.
        It reports no research-size minimum and does not reserve or dispatch.
        """
        parsed = parse_specs(
            specs, minimum=research_api.MULTI_MIN_CHILDREN,
            maximum=MAX_MULTI_BATCH,
            require_proposal_ids=True,
        )
        if parsed is None:
            return _invalid_result(owner="research_api.research_batch_status")
        return read_facade(
            "research_batch_status", parsed, state_dir=state_dir,
        )

    @server.tool(annotations=remote_read)
    def list_datasets() -> dict[str, Any]:
        """[READ_ONLY] List live datasets in the authorized account scope."""
        return read_facade("list_datasets")

    @server.tool(annotations=remote_read)
    def list_datafields(dataset_id: str, limit: int = 20, offset: int = 0, field_type: str | None = None) -> dict[str, Any]:
        """[READ_ONLY] Read one bounded live datafield page."""
        return read_facade("list_datafields", dataset_id, limit=limit, offset=offset, field_type=field_type)

    @server.tool(annotations=local_read)
    def list_templates(offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """[READ_ONLY] List one page of validated templates from the configured private catalog.

        Use to choose an existing template and review its field/slot contract before
        calling generate_probes. This reads the same private catalog used by the
        generator; a missing catalog is reported as unavailable with no public
        synthetic-catalog fallback. Expressions and fixed field bindings are omitted.
        """
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_LIST_ITEMS):
            return _invalid_result(owner="research_api.list_templates")
        owner = "research_api.list_templates"
        try:
            rows = api.list_templates(require_private=True, config=config)
        except Exception as exc:
            return _private_catalog_failure(exc, owner=owner)
        if not isinstance(rows, (list, tuple)) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            return _invalid_result(owner=owner, failure_code="INVALID_TEMPLATE_CATALOG")
        page = rows[offset:offset + limit]
        summaries = [
            {key: row[key] for key in _TEMPLATE_INVENTORY_KEYS if key in row}
            for row in page
        ]
        return _envelope({
            "source": "LOCAL_PRIVATE_CATALOG",
            "status": "AVAILABLE",
            "evidence_status": "AVAILABLE",
            "count": len(rows),
            "offset": offset,
            "limit": limit,
            "complete": offset + len(page) >= len(rows),
            "next_offset": None if offset + len(page) >= len(rows) else offset + len(page),
            "templates": summaries,
        }, owner=owner, list_item_limit=limit)

    @server.tool(annotations=remote_read)
    def generate_probes(
        template_ids: list[str], count: int, fields: list[dict[str, Any]],
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        """[READ_ONLY] Generate candidate SimulationSpecs from explicitly selected inputs.

        Supply template IDs, a count, and BRAIN field rows with their dataset
        provenance. Uses current operator capability and the configured private
        catalog. It does not rank/select fields or templates, validate admission,
        or submit Simulations; next retain unique proposal_id values, validate, then
        call research_batch_status before the appropriate Simulation write tool.
        """
        if (not isinstance(template_ids, list) or not 1 <= len(template_ids) <= MAX_MULTI_BATCH
                or isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= MAX_MULTI_BATCH
                or not isinstance(fields, list) or not fields
                or len(fields) > MAX_MULTI_BATCH
                or any(not isinstance(row, (Mapping, str)) for row in fields)):
            return _invalid_result(owner="research_api.generate_probes")
        owner = "research_api.generate_probes"
        try:
            specs = api.generate_probes(
                template_ids=template_ids, count=count, fields=fields,
                dataset_id=dataset_id, client=resolve_client(), config=config,
            )
        except Exception as exc:
            return _private_catalog_failure(exc, owner=owner, source="BRAIN_LIVE")
        if not isinstance(specs, (list, tuple)) or len(specs) > MAX_MULTI_BATCH:
            return _invalid_result(owner=owner, failure_code="INVALID_GENERATOR_RESULT")
        payload_specs = [_candidate_spec_payload(spec) for spec in specs]
        if any(spec is None for spec in payload_specs):
            return _invalid_result(owner=owner, failure_code="INVALID_GENERATOR_RESULT")
        return _envelope({
            "source": "BRAIN_LIVE",
            "catalog_source": "LOCAL_PRIVATE_CATALOG",
            "status": "AVAILABLE",
            "evidence_status": "AVAILABLE",
            "requested_count": count,
            "generated_count": len(payload_specs),
            "specs": payload_specs,
        }, owner=owner, allow_expression=True, list_item_limit=MAX_MULTI_BATCH)

    @server.tool(annotations=remote_read)
    def get_operator_reference() -> dict[str, Any]:
        """[READ_ONLY] Read current live operator capability and syntax facts."""
        return read_facade("get_operator_reference")

    @server.tool(annotations=remote_read)
    def validate_simulation_spec(spec: dict[str, Any]) -> dict[str, Any]:
        """[READ_ONLY] Validate a SimulationSpec using the existing Gateway facade."""
        parsed = parse_specs([spec] if isinstance(spec, dict) else spec, minimum=1, maximum=1)
        if parsed is None:
            return _invalid_result(owner="research_api.validate_simulation_spec")
        return read_facade("validate_simulation_spec", parsed[0], state_dir=state_dir)

    @server.tool(annotations=simulation_write)
    def simulate_batch(specs: list[dict[str, Any]]) -> dict[str, Any]:
        """[REMOTE_WRITE] Start 1–50 BRAIN Simulations through SimulationGateway."""
        parsed = parse_specs(specs, minimum=1, maximum=MAX_SIMULATION_BATCH, require_proposal_ids=True)
        if parsed is None:
            return _invalid_result(owner="research_api.simulate_batch", access_mode="SIMULATION_WRITE", remote_write=True)
        return write_facade("simulate_batch", parsed)

    @server.tool(annotations=simulation_write)
    def simulate_multi_batch(specs: list[dict[str, Any]]) -> dict[str, Any]:
        """[REMOTE_WRITE] Dispatch meaningful candidates through current bounded Multi capacity.

        Send a two-or-more-child candidate set that admission marked READY.
        There is no research-volume minimum; use simulate_batch for a single
        candidate. Gateway groups, queues, and limits actual parent dispatches.
        """
        parsed = parse_specs(
            specs, minimum=research_api.MULTI_MIN_CHILDREN,
            maximum=MAX_MULTI_BATCH, require_proposal_ids=True,
        )
        if parsed is None:
            return _invalid_result(owner="research_api.simulate_multi_batch", access_mode="SIMULATION_WRITE", remote_write=True)
        return write_facade("simulate_multi_batch", parsed)

    @server.tool(annotations=remote_read)
    def get_alpha_evidence(
        alpha_id: str, recordsets: list[str] | None = None,
        depth: str = "summary",
    ) -> dict[str, Any]:
        """[READ_ONLY] Read summary evidence, or full finalist evidence on request.

        Full depth includes the existing self-correlation read; selected
        recordsets continue to imply full depth.
        """
        selected = recordsets or []
        if (not alpha_id or len(alpha_id) > 128 or len(selected) > MAX_RECORDSETS
                or depth not in {"summary", "full"}):
            return _invalid_result(owner="research_api.get_alpha_evidence")
        return read_facade(
            "get_alpha_evidence", alpha_id, live=True, recordsets=selected,
            depth="full" if selected else depth, allow_expression=True,
        )

    @server.tool(annotations=remote_read)
    def compare_alphas(
        alpha_ids: list[str], max_concurrent: int = 4, max_pairs: int = 20,
    ) -> dict[str, Any]:
        """[READ_ONLY] Compare explicit candidates using pairwise live daily-PnL correlation.

        Returns strongest absolute pair correlations, overlap/sample evidence,
        and per-Alpha maximums. This is distinct from Alpha self-correlation,
        PROD correlation, and structural similarity. It sets no acceptance
        threshold; absent or insufficient PnL overlap stays UNKNOWN.
        """
        if (not isinstance(alpha_ids, list) or len(alpha_ids) < 2
                or len(alpha_ids) > research_api.MAX_PAIRWISE_ALPHA_IDS
                or any(not isinstance(alpha_id, str) or not alpha_id.strip()
                       or len(alpha_id.strip()) > 128 for alpha_id in alpha_ids)):
            return _invalid_result(owner="research_api.compare_alphas")
        normalized_ids = [alpha_id.strip() for alpha_id in alpha_ids]
        if (len(set(normalized_ids)) != len(normalized_ids)
                or isinstance(max_concurrent, bool)
                or not isinstance(max_concurrent, int)
                or not 1 <= max_concurrent <= 4
                or isinstance(max_pairs, bool)
                or not isinstance(max_pairs, int)
                or not 1 <= max_pairs <= 20):
            return _invalid_result(owner="research_api.compare_alphas")
        try:
            payload = api.compare_alphas(
                normalized_ids, client=resolve_client(), config=config,
                pairwise_pnl=True, max_concurrent=max_concurrent,
                max_pairs=max_pairs,
            )
        except Exception as exc:
            return _failed_result(exc, owner="research_api.compare_alphas")
        if not isinstance(payload, Mapping):
            return _invalid_result(
                owner="research_api.compare_alphas",
                failure_code="INVALID_CORRELATION_RESULT",
            )
        return _envelope(
            payload, owner="research_api.compare_alphas",
            list_item_limit=MAX_MULTI_BATCH,
        )

    @server.tool(annotations=remote_read)
    def get_alpha_prod_correlation(alpha_id: str) -> dict[str, Any]:
        """[READ_ONLY][FINALIST_ONLY] Read PROD correlation when explicitly requested for a finalist."""
        if not alpha_id or len(alpha_id) > 128:
            return _invalid_result(owner="research_api.get_alpha_prod_correlation")
        return read_facade("get_alpha_prod_correlation", alpha_id)

    @server.tool(annotations=remote_read)
    def reconcile_execution(fingerprint: str) -> dict[str, Any]:
        """[READ_ONLY] Reconcile one existing guard; never submit again."""
        return read_facade("reconcile_execution", fingerprint, state_dir=state_dir)

    @server.tool(annotations=local_read)
    def get_pending_executions() -> dict[str, Any]:
        """[READ_ONLY] Inspect unresolved ExecutionGuard rows only when diagnosis needs row detail."""
        try:
            payload = _pending_guard_payload(
                api, state_dir=state_dir, config=config
            )
        except Exception as exc:  # Do not expose paths or raw local-state errors.
            return _failed_result(
                exc, owner="research_api.get_pending_executions",
                source="LOCAL_EXECUTION_GUARD",
            )
        return _envelope(payload, owner="research_api.get_pending_executions")

    return server


def research_main() -> None:
    """Run the explicitly enabled Research MCP server on stdio."""
    try:
        server = build_research_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    server.run(transport="stdio")


def main() -> None:
    """Run the optional MCP server on stdio; stdout is reserved for MCP frames."""
    try:
        server = build_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    main()
