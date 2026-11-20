"""How a moment is written next to a message and in an export of the conversation.

Pure functions, no Flet. The backend sends the time of a message in UTC; the screen
shows it in the time of this computer (``astimezone()``), the shortest way that is
still clear: only the clock for today, with the day for an older message.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# The month names the way a date is read in Bulgarian: "14 ноември".
MONTHS = (
    "януари",
    "февруари",
    "март",
    "април",
    "май",
    "юни",
    "юли",
    "август",
    "септември",
    "октомври",
    "ноември",
    "декември",
)


def now() -> datetime:
    """The present moment in the time of this computer (a function, so tests can fix it)."""
    return datetime.now().astimezone()


def when_label(moment: datetime, at: datetime | None = None) -> str:
    """The time of a message for the screen: "14:05", "вчера 14:05", "14 ноември, 14:05".

    Only the clock for a message of today (``at``, the present moment, unless given),
    "вчера" for yesterday's, the day and month for the rest of the year, and the whole
    date - "14.11.2025 14:05" - for an older one.
    """
    local = moment.astimezone()
    today = (at or now()).astimezone().date()
    day = local.date()
    clock = f"{local:%H:%M}"
    if day == today:
        return clock
    if day == today - timedelta(days=1):
        return f"вчера {clock}"
    if day.year == today.year:
        return f"{day.day} {MONTHS[day.month - 1]}, {clock}"
    return f"{local:%d.%m.%Y} {clock}"


def full_label(moment: datetime) -> str:
    """The time with the whole date, for a file: "20.11.2026 14:05"."""
    return f"{moment.astimezone():%d.%m.%Y %H:%M}"
