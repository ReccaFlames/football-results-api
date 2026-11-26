"""Parsing and encoding rules shared by the v1.1 read endpoints (cursor, fields, filters)."""

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel

from app.models import ACCESS_REQUEST_STATUSES, MATCH_STATUSES, SCRAPE_RUN_STATUSES

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CURSOR_VERSION = 1


class ContractError(ValueError):
    """A request parameter is invalid.

    ``problem_type`` is a slug of ``PROBLEM_TYPES``; ``detail`` holds ``message`` plus problem
    extension members (``parameter``, ``invalid``, ``valid_values``, ``hint``, ``examples``).
    """

    def __init__(self, problem_type: str, detail: dict[str, Any]) -> None:
        super().__init__(detail["message"])
        self.problem_type = problem_type
        self.detail = detail


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_fields(
    value: str | None, model: type[BaseModel], always: Iterable[str]
) -> set[str] | None:
    """Return the selected top-level fields, or ``None`` for all fields."""
    if value is None:
        return None
    valid = list(model.model_fields)
    requested = _split(value)
    invalid = [item for item in requested if item not in valid]
    if invalid or not requested:
        raise ContractError(
            "invalid-parameter",
            {
                "message": "Unknown fields requested" if invalid else "fields must not be empty",
                "parameter": "fields",
                "invalid": invalid,
                "valid_values": valid,
            },
        )
    return set(requested) | set(always)


def parse_statuses(
    value: str | None, valid: tuple[str, ...] = MATCH_STATUSES, kind: str = "match"
) -> tuple[str, ...]:
    if value is None:
        return ()
    requested = _split(value)
    invalid = [item for item in requested if item not in valid]
    if invalid or not requested:
        raise ContractError(
            "invalid-parameter",
            {
                "message": f"Unknown {kind} status",
                "parameter": "status",
                "invalid": invalid,
                "valid_values": list(valid),
            },
        )
    return tuple(sorted(set(requested)))


def parse_job_statuses(value: str | None) -> tuple[str, ...]:
    return parse_statuses(value, SCRAPE_RUN_STATUSES, "job")


def parse_access_request_statuses(value: str | None) -> tuple[str, ...]:
    return parse_statuses(value, ACCESS_REQUEST_STATUSES, "access request")


def parse_date_bound(value: str | None, parameter: str, *, upper: bool) -> datetime | None:
    """Parse an ISO date or datetime; naive values are UTC.

    Lower bounds are inclusive. Upper bounds are returned exclusive: a date covers the whole UTC
    day and a datetime includes that instant.
    """
    if value is None:
        return None
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            moment = datetime.combine(day, time.min, tzinfo=UTC)
            return moment + timedelta(days=1) if upper else moment
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractError(
            "invalid-parameter",
            {
                "message": f"{parameter} must be an ISO 8601 date or datetime",
                "parameter": parameter,
                "invalid": [value],
                "examples": ["2026-08-01", "2026-08-01T18:00:00+02:00"],
            },
        ) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment + timedelta(microseconds=1) if upper else moment


def filter_hash(filters: dict[str, Any]) -> str:
    canonical = json.dumps(filters, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(key: list[Any], filters_hash: str) -> str:
    payload = json.dumps({"v": CURSOR_VERSION, "k": key, "f": filters_hash}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _invalid_cursor() -> ContractError:
    return ContractError(
        "invalid-cursor",
        {
            "message": "Invalid cursor",
            "parameter": "cursor",
            "hint": "Pass next_cursor from a previous response unchanged, or omit cursor.",
        },
    )


def decode_cursor(token: str, filters_hash: str, length: int) -> list[Any]:
    """Return the keyset stored in ``token``; it must belong to the same filters."""
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error) as error:
        raise _invalid_cursor() from error
    key = payload.get("k") if isinstance(payload, dict) else None
    if payload.get("v") != CURSOR_VERSION or not isinstance(key, list) or len(key) != length:
        raise _invalid_cursor()
    if payload.get("f") != filters_hash:
        raise ContractError(
            "invalid-cursor",
            {
                "message": "The cursor belongs to a request with different filters or order",
                "parameter": "cursor",
                "hint": "Repeat the original filters with this cursor, or omit cursor to restart.",
            },
        )
    return key


def match_keyset(token: str, filters_hash: str) -> tuple[datetime | None, int]:
    kickoff, match_id = decode_cursor(token, filters_hash, 2)
    if not isinstance(match_id, int) or isinstance(match_id, bool):
        raise _invalid_cursor()
    if kickoff is None:
        return None, match_id
    if not isinstance(kickoff, str):
        raise _invalid_cursor()
    try:
        parsed = datetime.fromisoformat(kickoff)
    except ValueError as error:
        raise _invalid_cursor() from error
    if parsed.tzinfo is None:
        raise _invalid_cursor()
    return parsed, match_id


def slug_keyset(token: str, filters_hash: str) -> str:
    (slug,) = decode_cursor(token, filters_hash, 1)
    if not isinstance(slug, str):
        raise _invalid_cursor()
    return slug


def id_keyset(token: str, filters_hash: str) -> int:
    (item_id,) = decode_cursor(token, filters_hash, 1)
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise _invalid_cursor()
    return item_id


def compact(model: BaseModel, fields: set[str] | None) -> dict[str, Any]:
    return model.model_dump(mode="json", include=fields)
