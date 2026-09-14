"""Pure codecs for ExperienceMemory's derived durable view."""

import base64
import binascii
import json
import zlib

from .expression import canonical_expression


def dict_list(value):
    """Normalize optional persisted collections at the trust boundary."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def stable_payload(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                      separators=(",", ":"))


def pack_expressions(expressions):
    raw = json.dumps(
        sorted(expressions), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def unpack_expressions(packed):
    try:
        raw = zlib.decompress(base64.b64decode(packed.encode("ascii")))
        values = json.loads(raw.decode("utf-8"))
        return (
            {canonical_expression(value) for value in values}
            if isinstance(values, list) else set()
        )
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error,
            zlib.error, json.JSONDecodeError):
        return set()
