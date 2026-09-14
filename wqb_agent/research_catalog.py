"""Pure deterministic fallback research catalog."""

from __future__ import annotations

from copy import deepcopy

SEED_HYPOTHESES = [
    {"id": "h-seed-reversal", "statement": "Short-term return reversal: stocks that rose sharply over the last 5 days tend to revert in the near term.", "tags": ["reversal", "return", "price", "short-term"], "direction": "reversal", "datasets": ["pv1", "pv13"]},
    {"id": "h-seed-analyst", "statement": "Analyst target-price revisions upward predict short-term outperformance.", "tags": ["analyst", "forecast", "revision", "target"], "direction": "long", "datasets": ["analyst4"]},
    {"id": "h-seed-option", "statement": "Stocks with elevated implied volatility earn lower forward returns.", "tags": ["option", "volatility", "implied", "risk"], "direction": "reversal", "datasets": ["option8", "option9"]},
    {"id": "h-seed-model", "statement": "High model risk scores predict lower forward returns.", "tags": ["model", "score", "risk", "composite"], "direction": "reversal", "datasets": ["model16", "model51"]},
    {"id": "h-seed-news", "statement": "Positive news sentiment predicts short-term positive returns.", "tags": ["news", "sentiment", "positive"], "direction": "long", "datasets": ["news18", "news12"]},
    {"id": "h-seed-fundamental", "statement": "Firms with strong earnings growth continue to outperform.", "tags": ["fundamental", "growth", "earning"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
]

EXPLORATION_HYPOTHESES = [
    {"id": "h-explore-fund-cashflow", "statement": "Strong operating cash flow quality predicts outperformance.", "tags": ["cashflow", "operating", "quality"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-leverage", "statement": "High leverage and debt burden predict lower forward returns.", "tags": ["debt", "leverage", "liability"], "direction": "reversal", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-value", "statement": "Low valuation relative to book value or enterprise value predicts outperformance.", "tags": ["value", "book", "enterprise"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-assets", "statement": "Efficient asset utilization and profitability predict outperformance.", "tags": ["asset", "profitability", "efficiency"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-news-attention", "statement": "Abnormally high news attention is followed by short-term reversal.", "tags": ["attention", "buzz", "count"], "direction": "reversal", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-novelty", "statement": "Novel company news contains information that persists into future returns.", "tags": ["novelty", "novel", "unique"], "direction": "long", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-relevance", "statement": "Highly relevant company-specific news predicts short-term returns.", "tags": ["relevance", "relevant", "company"], "direction": "long", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-volume", "statement": "Extreme news volume reflects overreaction and predicts reversal.", "tags": ["volume", "story", "article"], "direction": "reversal", "datasets": ["news18", "news12"]},
]


def fallback_research_hypotheses():
    return deepcopy(EXPLORATION_HYPOTHESES + SEED_HYPOTHESES)


def seed_hypothesis(round_no):
    return deepcopy(SEED_HYPOTHESES[int(round_no) % len(SEED_HYPOTHESES)])


def exploration_hypothesis(round_no):
    index = max(0, int(round_no) - 1) % len(EXPLORATION_HYPOTHESES)
    return deepcopy(EXPLORATION_HYPOTHESES[index])
