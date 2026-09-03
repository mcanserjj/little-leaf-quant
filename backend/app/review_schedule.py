from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import RUNTIME_DIR
from .execution import MARKET_HOLIDAYS
from .storage import now_iso, read_json, write_json


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_PATH = RUNTIME_DIR / "weekly-review.json"


def is_trading_day(day: date) -> bool:
    return day.year in MARKET_HOLIDAYS and day.weekday() < 5 and day not in MARKET_HOLIDAYS[day.year]


def shifted_weekly_target(friday: date) -> date | None:
    if friday.weekday() != 4 or friday.year not in MARKET_HOLIDAYS:
        return None
    target = friday
    for _ in range(10):
        if is_trading_day(target):
            return target
        target += timedelta(days=1)
    return None


def due_target(at: datetime) -> str | None:
    local = at.replace(tzinfo=SHANGHAI) if at.tzinfo is None else at.astimezone(SHANGHAI)
    candidates = []
    for offset in range(15):
        day = local.date() - timedelta(days=offset)
        if day.weekday() == 4:
            target = shifted_weekly_target(day)
            if target:
                due = datetime.combine(target, time(16, 30), SHANGHAI)
                if due <= local:
                    candidates.append((due, day))
    if not candidates:
        return None
    _, friday = max(candidates)
    return friday.isoformat()


def status() -> dict[str, Any]:
    return read_json(STATE_PATH, {"state": "idle", "updatedAt": None, "lastCompletedTarget": None})


def record(state: str, target: str, **extra: Any) -> dict[str, Any]:
    value = {**status(), "state": state, "target": target, "updatedAt": now_iso(), **extra}
    write_json(STATE_PATH, value)
    return value
