"""Compatibility wrapper for the template-owned AlphaFactory.

Candidate generation and mutation are owned by ``alpha_factory`` and the
optimizer workflow.  This wrapper remains only because runtime composition
still exposes the existing ``builder.factory`` projection.
"""

from .alpha_factory import AlphaFactory


class CandidateBuilder:
    """Expose the canonical template factory without a second generator."""

    def __init__(self, neutralization="SUBINDUSTRY", catalog_path=None,
                 require_private=False):
        self.factory = AlphaFactory(
            neutralization=neutralization,
            catalog_path=catalog_path,
            require_private=require_private,
        )
