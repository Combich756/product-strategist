"""Validate calendar periods and pin the instant used for an analysis."""

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state import UserError, need, utc_now

OFFSET = r"[+-](?:[01]\d|2[0-3]):[0-5]\d"
DAY = r"\d{4}-\d{2}-\d{2}"
TIMESTAMP = rf"{DAY}T\d{{2}}:\d{{2}}(?::\d{{2}}(?:\.\d{{3}}(?:\d{{3}})?)?)?(?:Z|{OFFSET})"


def timestamp(value, field):
    error = f"{field}: нужен момент ISO 8601 со смещением, например 2026-10-23T12:00:00+03:00."
    need(isinstance(value, str) and re.fullmatch(TIMESTAMP, value), error)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise UserError(error) from None


def zone(name, field):
    need(isinstance(name, str) and name.strip(), f"{field}: укажите часовой пояс или смещение UTC.")
    if name == "UTC":
        return timezone.utc
    if re.fullmatch(OFFSET, name):
        return datetime.fromisoformat(f"2000-01-01T00:00:00{name}").tzinfo
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise UserError(f"{field}: неизвестный часовой пояс {name!r}. Укажите IANA-зону, UTC или смещение.") from None


def analysis_clock(as_of, timezone_name):
    instant = timestamp(utc_now() if as_of is None else as_of, "--analysis-at")
    if timezone_name is None:
        offset = instant.isoformat()[-6:]
        timezone_name = "UTC" if offset == "+00:00" else offset
    local = instant.astimezone(zone(timezone_name, "--timezone"))
    return {"as_of": local.isoformat(), "timezone": timezone_name, "periods": []}


def day(value, field):
    error = f"{field}: нужна календарная дата ГГГГ-ММ-ДД."
    need(isinstance(value, str) and re.fullmatch(DAY, value), error)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise UserError(error) from None


def date_bounds(start, end, field):
    bounds = {}
    if start is not None:
        bounds["start"] = day(start, f"{field}.start").isoformat()
    if end is not None:
        bounds["end"] = day(end, f"{field}.end").isoformat()
    need(len(bounds) < 2 or bounds["start"] <= bounds["end"], f"{field}: начало позже окончания.")
    return bounds


def validate_analysis(value):
    need(isinstance(value, dict), "analysis: ожидается объект с as_of и timezone.")
    instant = timestamp(value.get("as_of"), "analysis.as_of")
    timezone_ = zone(value.get("timezone"), "analysis.timezone")
    need(instant.utcoffset() == instant.astimezone(timezone_).utcoffset(),
         "analysis.as_of: смещение не соответствует analysis.timezone на эту дату.")
    periods = value.get("periods", [])
    need(isinstance(periods, list), "analysis.periods: ожидается список периодов.")
    labels = set()
    for period in periods:
        need(isinstance(period, dict), "analysis.periods: каждый период должен быть объектом.")
        label = period.get("label")
        need(isinstance(label, str) and label.strip() and label.strip() not in labels,
             "analysis.periods: нужны непустые неповторяющиеся label.")
        labels.add(label.strip())
        bounds = date_bounds(period.get("start"), period.get("end"), "analysis.periods")
        need(len(bounds) == 2, "analysis.periods: нужны даты start и end.")


def source_timing(period, data_as_of, valid_from, valid_through, timezone_name):
    result = {}
    if period is not None:
        result["period"] = date_bounds(*period, field="--period")
    if data_as_of is not None:
        cutoff = day(data_as_of, "--data-as-of") if re.fullmatch(DAY, data_as_of) else timestamp(data_as_of, "--data-as-of")
        result["data_as_of"] = cutoff.isoformat()
    validity = date_bounds(valid_from, valid_through, "Срок действия")
    if validity:
        result["validity"] = validity
    if timezone_name is not None:
        zone(timezone_name, "--timezone")
        result["timezone"] = timezone_name
    return result
