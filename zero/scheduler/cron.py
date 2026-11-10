"""
Minimal 5-field cron matcher (dependency-free).

Supports standard 5-field cron expressions:
    minute hour day-of-month month day-of-week
with `*`, lists `,`, ranges `-`, and step `/` (e.g. `*/5`, `0-30/10`).

Day-of-week accepts 0-7 (both 0 and 7 mean Sunday). When both day-of-month
and day-of-week are restricted, a day matches if either matches (standard
crontab OR semantics).
"""
import datetime

_LO = (0, 0, 1, 1, 0)
_HI = (59, 23, 31, 12, 7)


def _parse_term(term: str, lo: int, hi: int) -> callable:
    step = 1
    if "/" in term:
        base, step_s = term.split("/", 1)
        step = int(step_s)
    else:
        base = term

    if base == "*":
        return lambda v: lo <= v <= hi and (v - lo) % step == 0

    if "-" in base:
        a, b = (int(x) for x in base.split("-", 1))
        return lambda v: a <= v <= b and (v - a) % step == 0

    base = int(base)
    return lambda v: v == base


def _parse_field(expr: str, lo: int, hi: int) -> tuple[bool, callable]:
    """Return (restricted, matcher). restricted=False means '*'."""
    terms = [t.strip() for t in expr.split(",") if t.strip()]
    if not terms or terms == ["*"]:
        return False, (lambda _v: True)
    matchers = [_parse_term(t, lo, hi) for t in terms]

    def match(v: int) -> bool:
        return any(m(v) for m in matchers)

    return True, match


def _split(cron: str) -> list[str]:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression (need 5 fields): {cron!r}")
    return parts


def matches(cron: str, dt: datetime.datetime) -> bool:
    minute, hour, dom, month, dow = _split(cron)
    restr_m, fn_minute = _parse_field(minute, _LO[0], _HI[0])
    restr_h, fn_hour = _parse_field(hour, _LO[1], _HI[1])
    restr_d, fn_dom = _parse_field(dom, _LO[2], _HI[2])
    restr_mo, fn_month = _parse_field(month, _LO[3], _HI[3])
    restr_w, fn_dow = _parse_field(dow, _LO[4], _HI[4])

    if not fn_minute(dt.minute):
        return False
    if not fn_hour(dt.hour):
        return False
    if not fn_month(dt.month):
        return False

    dow_num = (dt.weekday() + 1) % 7  # 0=Sun .. 6=Sat
    dom_ok = fn_dom(dt.day)
    dow_ok = fn_dow(dow_num)

    if restr_d and restr_w:
        day_ok = dom_ok or dow_ok
    elif restr_d:
        day_ok = dom_ok
    elif restr_w:
        day_ok = dow_ok
    else:
        day_ok = True

    return day_ok


def next_run(cron: str, after: datetime.datetime, window_minutes: int = 1_048_576) -> datetime.datetime:
    """Return the next datetime strictly after `after` matching `cron`."""
    cur = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(window_minutes):
        if matches(cron, cur):
            return cur
        cur += datetime.timedelta(minutes=1)
    raise ValueError(f"No matching time for cron expression within window: {cron!r}")


def interval(cron: str, after: datetime.datetime,
             window_minutes: int = 1_048_576) -> datetime.timedelta:
    """相邻两次触发之间的间隔（取 `after` 之后最近的两个触发点）。

    维护任务用它把复盘/补跑窗口与 cron 同步：日任务约 24h、周任务约 7d。
    月任务受月份天数影响，取值会略长于上一个周期（只会多覆盖，不会漏）。
    """
    first = next_run(cron, after, window_minutes)
    second = next_run(cron, first, window_minutes)
    return second - first
