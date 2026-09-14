"""Pure bounded policy for derived ExperienceMemory tiers."""

import re
from functools import lru_cache

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


@lru_cache(maxsize=256)
def tokens(text):
    words = [word for word in re.split(r"[^a-z0-9]+", str(text).lower())
             if len(word) > 2 or word.isdigit()]
    for cjk in _CJK_RUN.findall(str(text)):
        if len(cjk) >= 2:
            words.extend([cjk] if len(cjk) <= 4
                         else [cjk[i:i + 2] for i in range(len(cjk) - 1)])
    return words


def similar(left, right, threshold=0.8):
    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens or not right_tokens:
        return False
    union = len(set(left_tokens) | set(right_tokens))
    return union > 0 and len(set(left_tokens) & set(right_tokens)) / union >= threshold


def promotion_allowed(entry, promote_hits):
    if not isinstance(entry, dict) or entry.get("kind") != "observation":
        return False
    confirmed = entry.get("confirmation_status") == "INDEPENDENT_CONFIRMED"
    if not confirmed and entry.get("hits", 0) < promote_hits:
        return False
    return confirmed or len(set(entry.get("lineages") or [])) >= 2


def expiration_partition(entries, now_round, window):
    kept, expired = [], []
    for entry in entries or ():
        (kept if now_round - entry.get("round", now_round) < window else expired).append(entry)
    return kept, expired
