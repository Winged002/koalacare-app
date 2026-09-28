"""Time handling, in one place.

Care is full of "what is happening today" questions, so timezones have to be
explicit rather than implied. Everything is stored as an aware UTC datetime and
translated to the circle's timezone only for display and for interpreting what
a person typed into a form.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

DEFAULT_TIMEZONE = "Europe/Berlin"

# A short, curated list. A care circle spans a handful of households, so a full
# IANA picker would be noise; anything else can still be stored by name.
COMMON_TIMEZONES = (
    "Europe/Berlin",
    "Europe/Vienna",
    "Europe/Zurich",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Warsaw",
    "Europe/Lisbon",
    "UTC",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value) -> datetime | None:
    """Normalise anything coming back out of storage to aware UTC."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def tz_for(name: str | None):
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def to_local(value, tzname: str | None):
    moment = as_utc(value)
    return moment.astimezone(tz_for(tzname)) if moment else None


def parse_local(value: str | None, tzname: str | None):
    """Read an HTML datetime-local value as wall-clock time in the circle's zone."""
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tz_for(tzname)).astimezone(UTC)
    return None


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def local_day(value, tzname: str | None) -> date | None:
    moment = to_local(value, tzname)
    return moment.date() if moment else None


def day_bounds_utc(day: date, tzname: str | None) -> tuple[datetime, datetime]:
    """The UTC window covering one local calendar day."""
    zone = tz_for(tzname)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    return start, start + timedelta(days=1)


def today_local(tzname: str | None) -> date:
    return utcnow().astimezone(tz_for(tzname)).date()


def format_local(value, tzname: str | None, fmt: str = "%a %d %b, %H:%M") -> str:
    moment = to_local(value, tzname)
    return moment.strftime(fmt) if moment else "—"


def format_day(value, tzname: str | None, fmt: str = "%a %d %b") -> str:
    moment = to_local(value, tzname)
    return moment.strftime(fmt) if moment else "—"


def relative_day(value, tzname: str | None) -> str:
    """'Today', 'Tomorrow', 'Yesterday' or a short date - how people actually talk."""
    moment = to_local(value, tzname)
    if not moment:
        return "—"
    today = utcnow().astimezone(tz_for(tzname)).date()
    delta = (moment.date() - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    return moment.strftime("%a %d %b")


def humanise_age(value) -> str:
    """'3 hours ago', '2 days ago' - for the change feed."""
    moment = as_utc(value)
    if not moment:
        return ""
    seconds = (utcnow() - moment).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} d ago"
    return moment.strftime("%d %b")
