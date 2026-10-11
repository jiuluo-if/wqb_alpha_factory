# AQAF quality scoring and Osmosis preview

This feature evaluates only explicitly named, freshly read Alphas and produces internal, read-only Osmosis suggestions. It does not submit Alphas, write Osmosis points, or run Simulations. `research_api` remains the only Agent-facing facade; scoring and allocation are deterministic pure calculations in `wqb_agent/alpha_quality.py` and `wqb_agent/osmosis_allocation.py`.

## Evidence and eligibility

The facade fetches live Alpha detail and explicit full evidence (`aggregates`, `pnl`, and `self_correlation`). An Alpha is confirmed submitted only when live detail contains a parseable `dateSubmitted`/`date_submitted` or the explicit status `SUBMITTED`. `checks=PASS`, an `ACTIVE` status alone, and absence from the retention-bounded local feed do not prove submission. Unconfirmed IDs return `SUBMISSION_UNKNOWN` and grade `U`.

Scope comes from live Alpha `settings.region`, `settings.universe`, and `settings.delay`. A scope missing any of these values is unknown. A calibration scope must match all three values exactly. No cache entry is treated as exhaustive submission history.

Every score carries metric value, score, status, reason code, source label and unit. Calibration is a caller-supplied frozen reference object and must include `version`, `reference_id`, `frozen_at`, `source`, `minimum_reference_count`, an exact `scope`, and per-metric `median`, `mad`, positive `scale_floor`, `unit`, adequate `sample_count`, and `source`. Current candidates are never used to construct or update that reference group. A missing reference, inadequate sample count, scope mismatch, or unit mismatch leaves the metric unavailable.

The production facade additionally requires unit declarations with provenance, for example `{"margin": {"unit": "bp", "source": "verified-account-export"}}`. The source is reported with the result. It is the caller's responsibility to supply evidence-backed unit metadata; absent declarations are `UNIT_UNKNOWN`. PnL risk metrics require both a PnL unit declaration and an exact expected trading-date sequence with a source. The last 252 observations must align exactly with that calendar, be finite, ordered and unique, and have non-zero volatility. Drawdown outputs are dimensionless `MDD_strength` and `CDaR_strength`; no dollar scaling is inferred.

Yearly stability uses rows marked `complete: true` or explicitly covering January 1 through December 31, excludes the current year, and requires at least three complete years. It is historical stability, not OOS evidence. Fitness remains a zero-weight diagnostic. The official self-correlation field is accepted only as a finite `correlation` in [-1, 1]; local pairwise PnL correlation is kept separate.

## Scoring

AQAF v3 uses eight weighted signals: Sharpe 25%, Returns 10%, MDD strength 14%, CDaR strength 6%, Margin 15%, Turnover 5%, annual Stability 15%, and BRAIN Self correlation 10%. Robust normalization uses the frozen median/MAD/floor and the configured direction. Turnover is scored against frozen type-specific budgets. Explicit economic types require a supplied type label and provenance; without one, only the documented 0.08–0.15 and 0.35–0.50 turnover interpolation among low-turnover, general and high-turnover weights is used.

The result reports `q_mid`, `q_low`, `q_high`, `coverage`, per-metric statuses, risk alerts, grade and calibration version. Missing metrics retain the neutral score center of 50 and widen the interval; missing values never become zero. A/B thresholds are the requested 75/90% and 60/80% conservative-score/coverage rules. Insufficient calibration, coverage below 50%, or unavailable Sharpe produces grade `U`.

## Osmosis allocation

Each call allocates a single explicit Region × Delay scope. The evaluator's calibration still matches Region × Universe × Delay so incomparable universes are not silently mixed. Internal points total exactly 10,000. The recommended raw weight combines the requested exponential transform of conservative quality, clipped inverse volatility, a bounded individual MDD-strength tilt, and an iterative diversification factor. Diversification uses shrinkage-adjusted daily PnL Pearson correlation and aligned cumulative-drawdown correlation; fewer than the configured 120 common observations, incompatible PnL units, or unmeasurable pair risk remains unknown. Unknown family provenance excludes that candidate instead of assuming it is unique. Default concentration limits are 15% per Alpha and 25% per supplied family; inadequate aggregate capacity returns `INFEASIBLE`.

Integer points use largest remainder with Alpha ID as the stable tie-breaker, then recheck integer constraints. Each response includes exclusions and reasons, eligible count, HHI, effective Alpha count, family concentration, PnL coverage, historical portfolio diagnostics, and cost sensitivity (currently `UNKNOWN`). With at least three overlapping windows, it reports descriptive changes in the risk-adjusted configuration while holding the current quality scores fixed; this is configuration sensitivity, not out-of-sample performance. Equal, quality-only and risk-adjusted plans use the same eligible set and constraints.

The current BRAIN account Osmosis rules are **UNKNOWN**. Public WorldQuant pages do not publish the claimed point total, minimum scope count, minimum Alphas per scope, or lock timing. The 100,000-point/weekly-lock claims found in a July 2026 Scribd repost are not authenticated account evidence and are not treated as platform contract. Platform point conversion is emitted only when the caller supplies a fresh `LIVE_VERIFIED` `BRAIN_ACCOUNT` rule observation; no MCP argument can promote an unverified repost to that status.

## Public facade

All four methods are in `research_api` and the opt-in `full` tool manifest. They are omitted from the default `core` profile:

```python
from datetime import date
from wqb_agent import research_api

result = research_api.evaluate_submitted_alpha(
    "<alpha-id>",
    calibration=frozen_calibration,
    metric_units={
        "returns": {"unit": "fraction", "source": "VERIFIED_SCHEMA"},
        "margin": {"unit": "bp", "source": "VERIFIED_SCHEMA"},
        "turnover": {"unit": "fraction", "source": "VERIFIED_SCHEMA"},
    },
    expected_trading_dates=calendar_dates,
    expected_trading_dates_source="VERIFIED_TRADING_CALENDAR",
    pnl_unit="USD", pnl_unit_source="BRAIN_RECORDSET_SCHEMA",
    as_of=date(2026, 10, 11),
)
```

- `evaluate_submitted_alpha(alpha_id, ...)` returns one submission status, scope, quality record, metric status map, provenance and diagnostics.
- `evaluate_submitted_alphas(alpha_ids, ...)` evaluates 1–100 unique explicit IDs and returns a bounded list. Per-Alpha date calendars and PnL unit declarations are keyed by Alpha ID. The full-profile MCP adapter accepts at most 20 IDs per call to keep its response within the transport's output limit.
- `preview_osmosis_allocation(alpha_ids, scope, ...)` fetches fresh evidence and returns one internal point plan for exactly that scope. `family_labels` must map IDs to `{id, source}` records.
- `compare_osmosis_allocation_plans(alpha_ids, scope, ...)` returns equal, quality-only and risk-adjusted historical plan comparisons.

The preview methods accept the same calibration/unit/calendar inputs, plus family labels and allocation options. Their outputs never include Alpha expressions or raw PnL series. The MCP `build_server(profile="full")` exposes these tools; the default `profile="core"` does not.

## Offline verification

The calculation modules use synthetic data only. Focused coverage is in `tests/test_alpha_quality.py`, `tests/test_osmosis_allocation.py`, `tests/test_aqaf_api.py`, and the MCP profile tests in `tests/test_mcp_server.py`. No verification path performs BRAIN POSTs.
