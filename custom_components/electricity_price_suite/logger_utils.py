"""Shared logger helpers for electricity_price_suite."""

from __future__ import annotations


def normalize_program_key(value: str | None) -> str | None:
    """Normalize a free-form program label into a stable program key."""

    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    normalized: list[str] = []
    for char in text:
        if char in {"ä", "ö", "ü"}:
            normalized.append({"ä": "a", "ö": "o", "ü": "u"}[char])
        elif char == "ß":
            normalized.append("ss")
        elif char.isalnum():
            normalized.append(char)
        else:
            normalized.append("_")
    program_key = "".join(normalized)
    while "__" in program_key:
        program_key = program_key.replace("__", "_")
    return program_key.strip("_") or None


def display_program_name(program_key: str) -> str:
    """Build a user-facing program name from a normalized program key."""

    return " ".join(part.capitalize() for part in program_key.split("_") if part)
