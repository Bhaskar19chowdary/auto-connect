"""
Declarative translation between the engine's contract and an application's own API (adapter without code).

A *spec* describes how to produce one value from a source document:

    "valueDate":   {"from": "value_date", "date": "%d.%m.%Y"}                 # ISO date -> 08.11.2026
    "amountMinor": {"from": "amount", "scale": 100, "type": "int"}           # 1234.56 -> 123456 (cents)
    "status":      {"from": "result", "map": {"NO_HIT": "CLEAR", "HIT": "BLOCKED"}, "default": "REVIEW_REQUIRED"}
    "lines":       {"from": "entries", "each": {"dr": "debit", "cr": "credit", "amt": {"from": "amount", "scale": 100}}}
    "methods":     {"const": [{"method": "LIMIT_SYSTEM"}]}
    "kyc_ok":      {"from": "data.party.kycState", "map": {"V": true, "E": false}}
    "name":        "data.party.legalName"                                    # a plain string is a source path

Keys: from (dotted path, list indexes allowed), const, default, map (value table), date (output strftime; input
ISO or `date_in`), scale (multiply as Decimal), round (decimals), type (int / str / decimal / float / bool), upper,
lower, each (spec applied to every list item), template (dict of specs -> object), required.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict

_MISSING = object()


class MappingError(ValueError):
    pass


def get_path(data: Any, path: str) -> Any:
    if path in ("", "$", "."):
        return data
    for part in path.split("."):
        if isinstance(data, list) and part.lstrip("-").isdigit():
            i = int(part)
            data = data[i] if -len(data) <= i < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
        if data is None:
            return None
    return data


def set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _to_date(value: Any, fmt_in: str = "") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if fmt_in:
        return datetime.strptime(text, fmt_in).date()
    return date.fromisoformat(text[:10])


def apply(spec: Any, source: Any) -> Any:
    """Produces one value from `source` according to `spec`."""
    if isinstance(spec, str):
        return get_path(source, spec)
    if not isinstance(spec, dict):
        return spec
    if "template" in spec:
        return build(spec["template"], source)
    if "const" in spec:
        value = spec["const"]
    elif "from" in spec:
        value = get_path(source, spec["from"])
    else:
        value = source
    if value is None:
        if "default" in spec:
            value = spec["default"]
        elif spec.get("required"):
            raise MappingError(f"required value '{spec.get('from')}' is missing")
        else:
            return None
    if "each" in spec:
        if not isinstance(value, list):
            raise MappingError(f"'{spec.get('from')}' is not a list")
        return [apply(spec["each"], item) for item in value]
    if "map" in spec:
        table = spec["map"]
        key = value if not isinstance(value, bool) else str(value).lower()
        if key in table:
            value = table[key]
        elif str(key) in table:
            value = table[str(key)]
        elif "default" in spec:
            value = spec["default"]
        else:
            raise MappingError(f"value '{value}' of '{spec.get('from')}' has no translation")
    if "date" in spec and value is not None:
        d = _to_date(value, spec.get("date_in", ""))
        value = d.isoformat() if spec["date"] == "iso" else d.strftime(spec["date"])
    elif "date_in" in spec and value is not None:
        value = _to_date(value, spec["date_in"]).isoformat()
    if "scale" in spec and value is not None:
        value = Decimal(str(value)) * Decimal(str(spec["scale"]))
    if "round" in spec and value is not None:
        value = Decimal(str(value)).quantize(Decimal(1).scaleb(-int(spec["round"])), rounding=ROUND_HALF_UP)
    if spec.get("encode") == "base64" and value is not None:
        import base64

        value = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    if spec.get("upper") and isinstance(value, str):
        value = value.upper()
    if spec.get("lower") and isinstance(value, str):
        value = value.lower()
    if "type" in spec and value is not None:
        t = spec["type"]
        if t == "int":
            value = int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        elif t == "str":
            value = str(value)
        elif t == "decimal":
            value = str(Decimal(str(value)))
        elif t == "float":
            value = float(value)
        elif t == "bool":
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "y", "j", "ja")
    return value


def build(template: Dict[str, Any], source: Any) -> Dict[str, Any]:
    """Builds an object: every key of the template is a target path, every value a spec."""
    out: Dict[str, Any] = {}
    for target, spec in template.items():
        value = apply(spec, source)
        if value is not None:
            set_path(out, target, value)
    return out


def translate_error(error_spec: Dict[str, Any], body: Any, status: int) -> str:
    """Engine error code for an application's error answer, e.g. {"path": "error.code", "map": {...}}."""
    code = get_path(body, error_spec.get("path", "code")) if isinstance(body, (dict, list)) else None
    table = error_spec.get("map", {})
    return table.get(str(code)) or table.get(str(status)) or ""
