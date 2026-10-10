"""Display countdowns from fixed timestamps, never estimated resource values."""

from datetime import datetime
from math import ceil

from hoyo_cli.models.common import require_aware


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    minutes = seconds // 60
    if minutes == 0:
        return "<1m"
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d" + (f" {hours}h" if hours else "")
    if hours:
        return f"{hours}h" + (f" {minutes}m" if minutes else "")
    return f"{minutes}m"


def format_local_timestamp(at: datetime, now: datetime) -> str:
    require_aware(at)
    require_aware(now)
    local = at.astimezone(now.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if abs((local.date() - now.date()).days) < 7:
        weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()]
        return f"{weekday} {local:%H:%M}"
    month = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )[local.month - 1]
    date = month + f" {local.day}"
    if local.year != now.year:
        date += f" {local.year}"
    return f"{date} · {local:%H:%M}"


def format_full_at(full_at: datetime | None, now: datetime) -> str:
    require_aware(now)
    require_aware(full_at)
    if full_at is None:
        return "Unavailable"
    seconds = ceil((full_at - now).total_seconds())
    if seconds <= 0:
        return "Full"
    return (
        f"Full in {format_duration(seconds)} · {format_local_timestamp(full_at, now)}"
    )
