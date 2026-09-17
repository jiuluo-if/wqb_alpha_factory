"""Single source of truth for lightweight expression identity."""

import hashlib
import json
import re
from dataclasses import dataclass

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_OPERATOR_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NON_FIELD_IDENTIFIERS = {
    "abs", "add", "and", "bucket", "densify", "divide", "group_backfill",
    "group_mean", "group_neutralize", "group_rank", "group_scale",
    "group_zscore", "if_else", "inverse", "is_nan", "kth_element", "log",
    "max", "min", "multiply", "normalize", "not", "or", "power",
    "quantile", "rank", "reverse", "scale", "sign", "signed_power", "sqrt",
    "subtract", "trade_when", "days_from_last_change", "last_diff_value",
    "ts_arg_max", "ts_arg_min", "ts_av_diff", "ts_backfill", "ts_corr",
    "ts_count_nans", "ts_covariance", "ts_decay_linear", "ts_delay", "ts_delta",
    "ts_mean", "ts_product", "ts_quantile", "ts_rank", "ts_regression",
    "ts_scale", "ts_step", "ts_std_dev", "ts_sum", "ts_zscore", "vec_avg",
    "vec_sum", "winsorize", "zscore", "driver", "gaussian", "cauchy",
    "uniform", "filter", "dense", "constant", "longscale", "shortscale",
    "std", "rate", "hump", "ignore", "range", "sigma", "subindustry",
    "industry", "sector", "market", "lookback", "true", "false", "null",
}


_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![A-Za-z0-9_.])"
)


@dataclass(frozen=True)
class ExpressionAnalysis:
    """Deterministic, read-only facts extracted from an Alpha expression."""

    original: str
    canonical: str
    operators: tuple
    identifiers: tuple
    fields: tuple


def analyze_expression(expression, known_fields=None):
    """Return one shared expression analysis for validation and routing.

    ``known_fields`` is optional because callers that only need operator or
    identity facts should not have to perform discovery first.  Field matching
    is case-insensitive and boundary-aware, so ``close`` is not reported from
    ``close_5d``.  The returned tuples are sorted for stable evidence and
    cache keys.
    """
    original = str(expression or "")
    identifiers = tuple(sorted(
        set(_IDENTIFIER_RE.findall(original)), key=str.casefold,
    ))
    operators = tuple(sorted(
        {value.lower() for value in _OPERATOR_RE.findall(original)},
    ))
    fields = set()
    for value in known_fields or ():
        field = str(value or "")
        if not field:
            continue
        pattern = rf"(?<![\w]){re.escape(field)}(?![\w])"
        if re.search(pattern, original, flags=re.IGNORECASE):
            fields.add(field)
    return ExpressionAnalysis(
        original=original,
        canonical=canonical_expression(original),
        operators=operators,
        identifiers=identifiers,
        fields=tuple(sorted(fields, key=str.casefold)),
    )


def expression_field_identifiers(analysis):
    """Return identifiers which are not known operators or syntax helpers."""
    if not isinstance(analysis, ExpressionAnalysis):
        return ()
    return tuple(
        identifier for identifier in analysis.identifiers
        if identifier.lower() not in _NON_FIELD_IDENTIFIERS
    )


def canonical_expression(expression):
    """Normalize only syntax-insensitive whitespace/case for identity keys."""
    return _SPACE_RE.sub("", str(expression or "")).lower()


def expression_identity_keys(expression):
    """Return strict and numeric-abstracted advisory expression keys.

    Both keys preserve operator topology and abstract field identifiers.  The
    second key additionally replaces numeric literals with ``NUMBER``; it is
    a variant-family hint, not semantic or execution equivalence.
    """
    analysis = analyze_expression(expression)
    operators = set(analysis.operators)
    skeleton = _IDENTIFIER_RE.sub(
        lambda match: match.group(0) if match.group(0).casefold() in operators else "FIELD",
        analysis.canonical,
    )
    family_skeleton = _NUMBER_RE.sub("NUMBER", skeleton)
    return (
        hashlib.sha256(skeleton.encode("utf-8")).hexdigest(),
        hashlib.sha256(family_skeleton.encode("utf-8")).hexdigest(),
    )


def variant_family_fingerprint(expression):
    """Return an advisory operator-topology family key.

    Numeric literals are deliberately abstracted.  This must never replace
    ``submission_fingerprint`` for duplicate or write-safety decisions.
    """
    return expression_identity_keys(expression)[1]


def submission_fingerprint(expression, settings):
    """Return the stable expression/settings identity used by checkpoints."""
    canonical = json.dumps(
        {"expression": canonical_expression(expression), "settings": settings},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
