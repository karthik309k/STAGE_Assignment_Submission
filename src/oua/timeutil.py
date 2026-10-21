"""Time helpers. All internal times are timezone-aware UTC; IST (UTC+5:30) is used for quiet hours and 'per day' rules."""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt):
    return dt.astimezone(IST).date()


def in_quiet_hours(dt, start_h=22, end_h=8):
    h = dt.astimezone(IST).hour
    return h >= start_h or h < end_h


def next_allowed_send(dt, earliest, start_h=22, end_h=8, preferred_minute=30):
    """Earliest send time >= max(dt, earliest) outside quiet hours (IST).
    Inside quiet hours -> end_h:preferred_minute IST of the same/next morning."""
    t = max(dt, earliest)
    if not in_quiet_hours(t, start_h, end_h):
        return t
    local = t.astimezone(IST)
    morning = local.replace(hour=end_h, minute=preferred_minute, second=0, microsecond=0)
    if local.hour >= start_h:
        morning += timedelta(days=1)
    return morning.astimezone(UTC)
