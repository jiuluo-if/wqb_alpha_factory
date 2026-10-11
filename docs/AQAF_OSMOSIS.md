# AQAF quality scoring and Osmosis preview

This feature evaluates only explicitly named, freshly read Alphas and produces internal, read-only Osmosis suggestions. It does not submit Alphas, write Osmosis points, or run Simulations. `research_api` remains the only Agent-facing facade; scoring and allocation are deterministic pure calculations in `wqb_agent/alpha_quality.py` and `wqb_agent/osmosis_allocation.py`.

## Evidence and eligibility

The facade first reads live Alpha detail. It reads `aggregates`, `pnl`, and `self_correlation` only after detail confirms submission. An Alpha is confirmed submitted only when live detail contains a parseable `dateSubmitted`/`date_submitted` or the explicit status `SUBMITTED`. `checks=PASS`, an `ACTIVE` status alone, and absence from the retention-bounded local feed do not prove submission. Unconfirmed IDs return `SUBMISSION_UNKNOWN` and grade `U` without deep evidence reads.

Scope comes from live Alpha `settings.region`, `settings.universe`, and `settings.delay`. A scope missing any of these values is unknown. A calibration scope must match all three values exactly. No cache entry is treated as exhaustive submission history.

Every score carries metric value, score, status, reason code, source label and unit. The offline `build_calibration` workflow accepts an explicit reference group and a held-out candidate ID. It filters out that candidate and every row whose Region, Universe, or Delay differs, then returns a frozen, versioned, JSON-serializable artifact containing robust medians/MADs, units, scale floors, per-metric sample counts, scope, freeze time, reference ID, and the exclusion policy. It retains no reference Alpha IDs or individual metrics. The scorer accepts only an `AVAILABLE` artifact whose held-out ID matches the evaluated Alpha. Missing or inadequate samples, incomplete metric references, scope mismatch, or unit mismatch remain `UNKNOWN`; no production calibration artifact is bundled in Git.

The production facade additionally requires unit declarations with provenance, for example `{"margin": {"unit": "bp", "source": "verified-account-export"}}`. The source is reported with the result. It is the caller's responsibility to supply evidence-backed unit metadata; absent declarations are `UNIT_UNKNOWN`. Direct PnL lists and official `schema.properties` + `records` payloads pass through one date/numeric decoder. PnL risk metrics require both a PnL unit declaration and an expected trading-date sequence with a source. The last 252 observations must align exactly with that calendar, be finite, ordered and unique, and have non-zero volatility. Drawdown outputs are dimensionless `MDD_strength` and `CDaR_strength`; no dollar scaling is inferred.

Yearly stability requires an official aggregate row plus a caller-supplied, provenance-labeled trading calendar and PnL coverage for every calendar trading date in that closed year. The code checks date completeness but cannot authenticate a caller's source label. A past year label or `complete: true` alone is insufficient; missing dates and years stay unavailable, and at least three fully covered prior years are required. It is historical stability, not OOS evidence. Fitness remains zero-weight and participates only in conservative sign consistency diagnostics; exact Fitness formula consistency remains unverified from public primary BRAIN documentation. Self-correlation status is checked at outer and nested envelopes; only explicitly named finite `correlation` values are read from known `is`, `data`, or direct response envelopes. `max` is not assumed synonymous and stays unknown without a schema contract. Local pairwise PnL correlation is kept separate.

## Scoring

AQAF v3 uses eight weighted signals: Sharpe 25%, Returns 10%, MDD strength 14%, CDaR strength 6%, Margin 15%, Turnover 5%, annual Stability 15%, and BRAIN Self correlation 10%. Robust normalization uses the frozen median/MAD/floor and the configured direction. Turnover is scored against frozen type-specific budgets. Explicit economic types require a supplied type label and provenance; without one, only the documented 0.08–0.15 and 0.35–0.50 turnover interpolation among low-turnover, general and high-turnover weights is used.

The result reports `q_mid`, `q_low`, `q_high`, `coverage`, per-metric statuses, economic diagnostics, risk alerts, grade and calibration version. Missing metrics retain the neutral score center of 50 and widen the interval; missing values never become zero. A/B thresholds are the requested 75/90% and 60/80% conservative-score/coverage rules. Insufficient calibration, coverage below 50%, or unavailable Sharpe produces grade `U`. Verified failed or unrecognized BRAIN Checks fail closed to grade `U`. Non-positive or out-of-calibration-range returns, negative Margin, strongly conflicting Sharpe/Returns signs, Fitness/Sharpe sign conflicts, and negative Turnover produce diagnostics and cap an otherwise-A grade at B. A defensive weight profile does not establish a crisis hedge.

## Osmosis allocation

Each call allocates a single explicit Region × Delay scope. The evaluator's calibration still matches Region × Universe × Delay so incomparable universes are not silently mixed. Internal points total exactly 10,000. The recommended raw weight combines the requested exponential transform of conservative quality, clipped inverse volatility, a bounded individual MDD-strength tilt, and an iterative diversification factor. Diversification uses shrinkage-adjusted daily PnL Pearson correlation and aligned cumulative-drawdown correlation; fewer than the configured 120 common observations, incompatible PnL units, or unmeasurable pair risk remains unknown. Unknown family provenance excludes that candidate instead of assuming it is unique. Default concentration limits are 15% per Alpha and 25% per supplied family; inadequate aggregate capacity returns `INFEASIBLE`.

Integer points use largest remainder with Alpha ID as the stable tie-breaker, then recheck integer constraints and the exact 10,000-point total. Each allocation reports its continuous `target_weight` and its actual `integerized_weight` separately. Non-convergent optimization, undefined correlation, or inadequate overlap yields no allocation. Portfolio diagnostics use the same requested minimum overlap as pairwise correlation. Each response includes exclusions and reasons, eligible count, HHI, effective Alpha count, family concentration, PnL coverage, historical portfolio diagnostics, and cost sensitivity (currently `UNKNOWN`). With at least three overlapping windows, it reports descriptive changes in the risk-adjusted configuration while holding the current quality scores fixed; this is configuration sensitivity, not out-of-sample performance. Equal, quality-only and risk-adjusted plans use the same eligible set and constraints.

The current BRAIN account Osmosis rules are **UNKNOWN**. Public WorldQuant pages do not publish the claimed point total, minimum scope count, minimum Alphas per scope, per-Alpha point constraints, or lock timing. No rule mapping supplied by a caller can authenticate its own source, acquisition time, scope, version, or account authority. Feasible results therefore remain `INTERNAL_PREVIEW`; platform point totals stay absent and account requirements are not asserted.

## Public facade

All five methods are in `research_api` and the opt-in `full` tool manifest. They are omitted from the default `core` profile. `build_aqaf_calibration` is a pure calculation over one explicitly supplied offline reference set; it does not read BRAIN or persist the reference rows:

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
- `build_aqaf_calibration(reference_rows, ...)` freezes the leave-one-candidate-out reference summary for a matching Region × Universe × Delay scope. Insufficient or malformed reference data returns `UNKNOWN`; only the artifact summary should be retained locally.
- `preview_osmosis_allocation(alpha_ids, scope, ...)` fetches fresh evidence and returns one internal point plan for exactly that scope. `family_labels` must map IDs to `{id, source}` records.
- `compare_osmosis_allocation_plans(alpha_ids, scope, ...)` returns equal, quality-only and risk-adjusted historical plan comparisons.

The preview methods accept the same calibration/unit/calendar inputs, plus family labels and allocation options. Their outputs never include Alpha expressions or raw PnL series. The MCP `build_server(profile="full")` exposes these tools; the default `profile="core"` does not.

## Offline verification

The calculation modules use synthetic data only. Focused coverage is in `tests/test_aqaf.py`, with MCP profile coverage in `tests/test_mcp_server.py`. No verification path performs BRAIN POSTs.
