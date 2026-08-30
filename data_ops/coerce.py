"""Explicit type coercion. Every conversion is reported. NULL ≠ empty string."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class CoercionError(ValueError):
    pass


def is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return False


def coerce_value(value: Any, sql_type: str, *, column: str) -> tuple[Any, str | None]:
    """Return (python_value, coercion_note_or_None). Raises CoercionError."""
    t = (sql_type or "").upper()
    if is_null(value):
        return None, None
    if isinstance(value, str) and value == "":
        # Empty string is distinct from NULL for text; for non-text it is invalid unless bool/int.
        if "CHAR" in t or "TEXT" in t or "CLOB" in t or t in ("", "VARCHAR"):
            return "", None
        if "BOOL" in t:
            raise CoercionError(f"{column}: empty string is not a boolean (NULL vs '' preserved)")
        raise CoercionError(f"{column}: empty string is not a valid {sql_type}")

    if "BOOL" in t:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(int(value)), f"{column}: numeric {value!r} -> {bool(int(value))}"
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("1", "true", "yes", "on", "y"):
                note = f"{column}: string {value!r} -> True" if s != "true" or value != "true" else None
                return True, note
            if s in ("0", "false", "no", "off", "n"):
                return False, f"{column}: string {value!r} -> False"
        raise CoercionError(f"{column}: cannot put {value!r} in a boolean column")

    if "INT" in t:
        if isinstance(value, bool):
            raise CoercionError(f"{column}: boolean {value!r} is not an integer")
        if isinstance(value, int):
            return value, None
        if isinstance(value, float):
            if value.is_integer():
                return int(value), f"{column}: float {value!r} -> {int(value)}"
            raise CoercionError(f"{column}: non-integral float {value!r}")
        if isinstance(value, str):
            s = value.strip()
            if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
                n = int(s[:-2]) if not s.startswith("-") else int(s[:-2])
                # handle negative
                n = int(Decimal(s))
                return n, f"{column}: string {value!r} (spreadsheet float) -> {n}"
            if s.lstrip("-").isdigit():
                return int(s), f"{column}: string {value!r} -> {int(s)}"
        raise CoercionError(f"{column}: cannot put {value!r} in an integer column")

    if any(x in t for x in ("REAL", "FLOA", "DOUB", "NUMER", "DECIM")):
        try:
            d = Decimal(str(value))
            return float(d), (f"{column}: {value!r} -> {float(d)}" if not isinstance(value, (int, float, Decimal)) else None)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise CoercionError(f"{column}: cannot put {value!r} in a numeric column") from exc

    if "DATE" in t and "TIME" not in t:
        if isinstance(value, datetime):
            return value.date().isoformat(), f"{column}: datetime -> date"
        if isinstance(value, date):
            return value.isoformat(), None
        s = str(value).strip()
        return s, None

    if "TIME" in t or t == "DATETIME" or t == "TIMESTAMP":
        if isinstance(value, datetime):
            return value.replace(tzinfo=None).isoformat(sep=" "), None
        return str(value), None

    # TEXT / BLOB / other
    if isinstance(value, (dict, list)):
        raise CoercionError(f"{column}: structured value not allowed in scalar column")
    return value if not isinstance(value, bytes) else value, None
