from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET_ZONE = ZoneInfo("America/New_York")


def et_now() -> datetime:
    """Current US/Eastern time as a naive datetime (handles EST/EDT automatically)."""
    return datetime.now(ET_ZONE).replace(tzinfo=None)


def utc_to_et(dt_utc: datetime) -> datetime:
    """Convert an aware-or-naive UTC datetime to naive US/Eastern."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(ET_ZONE).replace(tzinfo=None)


def parse_date(date_str: str) -> str | None:
    """Parse a user-supplied date string into YYYY-MM-DD (ET baseline). Returns None if blank/invalid."""
    now = et_now()
    if not date_str:
        return None

    date_str = date_str.lower().strip()
    if date_str == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str == "today":
        return now.strftime("%Y-%m-%d")
    elif date_str == "tomorrow":
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str.startswith("+") or date_str.startswith("-"):
        try:
            return (now + timedelta(days=int(date_str))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    elif "/" in date_str or "-" in date_str:
        parts = date_str.replace("-", "/").split("/")
        try:
            month, day = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) == 3 else now.year
            if year < 100:
                year += 2000
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, IndexError):
            pass

    return None
