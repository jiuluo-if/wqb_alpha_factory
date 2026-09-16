"""Static operator syntax reference used by the public research tools."""

import hashlib
import os
import re
from importlib import resources

FACTORY_BATCH_SIZE = 100


def _reference(text, path):
    return {
        "path": path,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "operators": sorted(set(re.findall(r"`([a-z][a-z0-9_]*)\s*\(", text))),
        "source": "STATIC_SYNTAX_REFERENCE",
        "availability": "UNKNOWN",
        "status": "UNKNOWN",
        "evidence_status": "UNAVAILABLE",
    }


def load_operator_syntax_reference(path):
    """Load evergreen syntax hints; never assert current capability truth."""
    path = os.path.abspath(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return _reference(text, path)


def load_packaged_operator_syntax_reference():
    """Load syntax hints from the installed package resource."""
    resource = resources.files("wqb_agent.reference").joinpath(
        "OPERATORS_CHEATSHEET.md"
    )
    return _reference(
        resource.read_text(encoding="utf-8"),
        "wqb_agent.reference/OPERATORS_CHEATSHEET.md",
    )


def _operator_reference(path):
    """Compatibility name for the explicit static reference loader."""
    return load_operator_syntax_reference(path)
