from datetime import datetime
from zoneinfo import ZoneInfo

from app.review_schedule import due_target, shifted_weekly_target


TZ = ZoneInfo("Asia/Shanghai")


def test_friday_holiday_shifts_to_next_trading_day_close():
    assert shifted_weekly_target(datetime(2026, 9, 25).date()).isoformat() == "2026-09-28"
    assert due_target(datetime(2026, 9, 28, 16, 29, tzinfo=TZ)) == "2026-09-18"
    assert due_target(datetime(2026, 9, 28, 16, 30, tzinfo=TZ)) == "2026-09-25"


def test_normal_friday_is_due_after_close():
    assert due_target(datetime(2026, 9, 18, 16, 29, tzinfo=TZ)) == "2026-09-11"
    assert due_target(datetime(2026, 9, 18, 16, 30, tzinfo=TZ)) == "2026-09-18"
