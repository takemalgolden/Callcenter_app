"""Dependency-free monthly workday assignment with strict weekly/day-off constraints.

Workweeks run Sunday-Saturday, including assigned days in adjacent months.
For approved full-day leave the caller must omit the day from candidates.
Finalized dates are compulsory and never edited.
"""
from datetime import timedelta
import calendar
from decimal import Decimal, ROUND_HALF_UP


def automatic_monthly_days(month, weekly_max):
    """Monthly contractual workdays, rounded half up (31 x 5 / 7 -> 22).

    `month` is YYYY-MM. This is a planning target, not a guarantee that
    individual weeks/cross-month commitments permit exactly that count.
    """
    year, number = map(int, month.split('-'))
    if not 1 <= weekly_max <= 7:
        raise ValueError('週最大勤務日数は1〜7日です。')
    days = calendar.monthrange(year, number)[1]
    return int((Decimal(days) * Decimal(weekly_max) / Decimal(7)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def monthly_target(month, weekly_max, monthly_max=None):
    """Target after an optional administrator-set monthly upper bound."""
    automatic = automatic_monthly_days(month, weekly_max)
    if monthly_max is None:
        return automatic
    if not 0 <= int(monthly_max) <= 31:
        raise ValueError('月間最大勤務日数は0〜31日です。')
    return min(automatic, int(monthly_max))


def select_workdays(days, candidates, fixed_dates, external_dates, target, weekly_max, max_consecutive, monthly_max=None):
    """Return (selected_dates, error_message).

    ``candidates[day]`` is (shift_pattern, day_score).  Score is a coverage
    heuristic; target count, weekly cap, consecutive cap, leave are hard.
    On impossible combinations, the returned partial dates are diagnostic only;
    the caller must never persist them.
    """
    if not days:
        return set(), None if target == 0 else '対象日がありません。'
    all_days = set(days)
    fixed_dates = set(fixed_dates) & all_days
    outside = set(external_dates) - all_days
    if not 0 <= target <= len(days):
        return fixed_dates, f'登録勤務日数{target}日は対象月の日数{len(days)}日を超えています。'
    if not 1 <= weekly_max <= 7 or not 1 <= max_consecutive <= 31:
        return fixed_dates, '週間上限または連勤上限の設定が不正です。'
    if monthly_max is not None and len(fixed_dates) > monthly_max:
        return fixed_dates, (f'確定済み勤務{len(fixed_dates)}日が月間最大勤務日数{monthly_max}日を超えています。'
                             '確定済みシフトか月間上限を確認してください。')
    min_target = max(target, len(fixed_dates))
    week_sunday = lambda d: d - timedelta(days=(d.weekday()+1)%7)
    outside_by_week = {}
    for d in outside:
        w = week_sunday(d)
        outside_by_week[w] = outside_by_week.get(w, 0) + 1
    begin = days[0]
    previous = 0
    while begin-timedelta(days=previous+1) in outside:
        previous += 1
        if previous > max_consecutive: break
    ending = days[-1]
    future = 0
    while ending+timedelta(days=future+1) in outside:
        future += 1
        if future > max_consecutive: break
    future_fixed = [sum(1 for d in days[i+1:] if d in fixed_dates) for i in range(len(days))]
    # count, streak, weekly_used -> (utility, bit-mask)
    states = {(0, previous, 0): (0, 0)}
    for i, day in enumerate(days):
        wk = week_sunday(day)
        first_week_day = i == 0 or wk != week_sunday(days[i-1])
        forced = day in fixed_dates
        pattern = candidates.get(day)
        upcoming = {}
        for (count, streak, weekly), (value, mask) in states.items():
            used = outside_by_week.get(wk, 0) if first_week_day else weekly
            choices = (True,) if forced else (False, True) if pattern else (False,)
            for working in choices:
                if working and not forced and (count + future_fixed[i] >= min_target or (monthly_max is not None and count >= monthly_max)):
                    continue
                if working and (used >= weekly_max or streak >= max_consecutive):
                    continue
                # Finalized work overlapping approved full-day leave is inconsistent.
                # The caller distinguishes this explicitly for an actionable error.
                new_count=count+int(working)
                new_streak=streak+1 if working else 0
                new_week=used+int(working)
                gain = 0 if forced or not working else int(pattern[1])
                key = (new_count,new_streak,new_week)
                new_value = value + gain - (2 if working and streak >= 3 else 0)
                old = upcoming.get(key)
                if old is None or new_value > old[0]:
                    upcoming[key]=(new_value,mask | (1 << i) if working else mask)
        states=upcoming
        if not states:
            return fixed_dates, f'{day.isoformat()}付近で、確定済み勤務と週最大{weekly_max}日／最大{max_consecutive}連勤の条件が矛盾しています。'
    states = {k:v for k,v in states.items() if k[1]+future <= max_consecutive}
    if not states:
        return fixed_dates, f'翌月の確定済み勤務との連勤上限（{max_consecutive}日）が矛盾しています。'
    best_key,best=max(states.items(),key=lambda kv:(kv[0][0],kv[1][0]))
    count=best_key[0]
    selected={day for i,day in enumerate(days) if best[1] & (1 << i)}
    if count < target:
        return selected, (f'登録{target}日に対し最大{count}日しか配置できません。'
                f'承認済み希望休・半休パターン・週最大{weekly_max}日・最大{max_consecutive}連勤・'
                '前後の月の確定シフトを確認してください。')
    return selected,None
