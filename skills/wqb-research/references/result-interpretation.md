# Result Interpretation

## Evidence depth

Use the Simulation's returned live Alpha detail or `get_alpha_summary()` for broad screening. Call `get_alpha_evidence(depth="full")` for a small finalist set only when the question requires aggregates, PnL, self-correlation or selected recordsets. Request `get_alpha_prod_correlation()` only for finalists; ordinary probes do not wait for it.

For a batch comparison, prefer the summary depth. A summary read is one Alpha-detail request; it must not fan out into PnL, yearly aggregates and correlation calls.

## Selection bias and multiple testing

- Interpret a result together with `variant_family`, `observed_execution_count`, structural similarity, yearly evidence, correlation, parameter neighborhood and the proposed mechanism.
- `observed_execution_count` is an observed lower bound, not a complete trial history. It is a warning about how many related attempts preceded a selected result, not an automatic score or filter.
- High Sharpe after many related trials is exposed to multiple-testing and selection bias. Look for evidence across years, siblings, settings and plausible parameter neighborhoods; do not promote a lone peak.
- Preserve exploration alongside promising directions. Expand only when comparative evidence supports a specific mechanism, and keep at least one competing or falsifying explanation alive.

## Failure attribution

Record the most likely category and its evidence: hypothesis, field, operator, horizon, implementation, correlation or robustness. Separate a platform/implementation failure from a negative economic result. Retry an ambiguous write only by reconciling its existing progress URL; `SUBMIT_UNKNOWN` without a URL never causes a replacement POST.
