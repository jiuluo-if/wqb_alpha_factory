"""Thin serialization objects for independent research evidence dimensions."""

from dataclasses import dataclass


def classify_research(quality, robustness, statistical, incremental, platform):
    values = {str(value or "").upper() for value in
              (quality, robustness, statistical, incremental, platform)}
    if "FAIL" in values or "REJECTED" in values:
        return "REJECTED"
    if str(quality or "").upper() in {"PROMISING", "STABLE", "PASS"} and str(robustness or "").upper() == "PASS":
        if str(statistical or "").upper() == "PASS" and str(platform or "").upper() == "PASS":
            if str(incremental or "").upper() in {"PASS", "UNAVAILABLE", "INCONCLUSIVE"}:
                return "PORTFOLIO_CANDIDATE" if str(incremental or "").upper() == "PASS" else "STABLE"
        return "STABLE"
    return "PROMISING" if str(quality or "").upper() in {"PROMISING", "STABLE"} else "REJECTED"


@dataclass(frozen=True)
class ResearchEvidenceBundle:
    quality: object
    robustness: object
    statistical: object
    incremental: object
    yearly: object
    platform: object

    @classmethod
    def from_parts(cls, quality, robustness, statistical, incremental, yearly, platform):
        return cls(quality, robustness, statistical, incremental, yearly, platform)

    def as_dict(self):
        return {
            "quality": self.quality,
            "robustness": self.robustness,
            "statistical": self.statistical,
            "incremental": self.incremental,
            "yearly": self.yearly,
            "platform": self.platform,
            "effective_trial_count": {
                "value": (self.quality or {}).get("effective_trial_count") if isinstance(self.quality, dict) else None,
                "method": "structural_cluster_proxy",
                "quality": "APPROXIMATE",
            },
        }
