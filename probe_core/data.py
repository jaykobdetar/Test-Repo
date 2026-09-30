"""Strict deterministic JSON for persisted task results."""

import json
import math
from typing import Any


def _check_json(value: Any, ancestors: set[int]) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if type(value) not in (dict, list):
        raise TypeError("Only JSON objects, arrays, strings, numbers, booleans and null are allowed")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Cyclic JSON values are not allowed")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise TypeError("JSON object keys must be strings")
            children = value.values()
        else:
            children = value
        for child in children:
            _check_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def canonical_json(value: Any) -> str:
    """Encode strict JSON deterministically, without ASCII escaping or NaN.

    This is the project's stable Python JSON encoding, not an RFC 8785 claim.
    Lone Unicode surrogates, non-string keys, tuples and custom types are rejected.
    """
    _check_json(value, set())
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result.encode("utf-8", errors="strict")
    return result
