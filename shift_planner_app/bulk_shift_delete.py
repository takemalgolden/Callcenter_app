"""Monthly shift removal with protected scope, concurrency check and break invalidation.

No employee, leave request, shift pattern or history tables are deleted.
All modifications share the caller's SQLite transaction.
"""
import re
from datetime import date, datetime


class BulkShiftDeleteError(ValueError):
    """A request cannot safely be applied; no changes should be committed."""


def validate_month(month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise BulkShiftDeleteError("対象月の形式が正しくありません。")
    try:
        year, number = (int(p) for p in month.split('-'))
        date(year, number, 1)
    except ValueError as exc:
        raise BulkShiftDeleteError("対象月が正しくありません。") from exc
    return month


def month_counts(conn, month):
    validate_month(month)
    counts = {'draft': 0, 'finalized': 0}
    rows = conn.execute("""SELECT status,COUNT(*) AS n FROM assignments
                           WHERE substr(work_date,1,7)=? GROUP BY status""", (month,))
    for row in rows:
        if row[0] in counts:
            counts[row[0]] = row[1]
    return counts


def remove_month(conn, *, month, scope, admin_username, expected_draft,
                 expected_finalized, refresh_day):
    """Remove selected month's shifts, keeping a traceable approval/audit record.

    Caller commits this transaction. `refresh_day` takes (conn, date, status).
    Related break runs are *invalidated*, not erased, so operators will never
    see obsolete confirmed breaks and auditors can still inspect prior runs.
    """
    validate_month(month)
    if scope not in ('draft', 'all'):
        raise BulkShiftDeleteError("削除範囲が不正です。")
    if not admin_username:
        raise BulkShiftDeleteError("管理者としてログインしてください。")
    try:
        expected_draft = int(expected_draft)
        expected_finalized = int(expected_finalized)
    except (TypeError, ValueError) as exc:
        raise BulkShiftDeleteError("件数の確認情報が不正です。再表示してやり直してください。") from exc
    if expected_draft < 0 or expected_finalized < 0:
        raise BulkShiftDeleteError("件数の確認情報が不正です。")

    # Lock first, then check the current counts. Avoid deleting newly created
    # shifts when another admin edits the month while the dialog is open.
    conn.execute('BEGIN IMMEDIATE')
    counts = month_counts(conn, month)
    if counts['draft'] != expected_draft or counts['finalized'] != expected_finalized:
        raise BulkShiftDeleteError('シフト件数が変更されました。カレンダーを再表示してからやり直してください。')

    clause = " AND status='draft'" if scope == 'draft' else ''
    rows = conn.execute("SELECT employee_id,work_date,status FROM assignments "
                        "WHERE substr(work_date,1,7)=?" + clause, (month,)).fetchall()
    if not rows:
        raise BulkShiftDeleteError('選択した範囲に削除可能なシフトがありません。')
    touched_days = sorted({row['work_date'] for row in rows})
    deleted_employees = {(row['work_date'], row['employee_id']) for row in rows}
    removed_draft = sum(row['status'] == 'draft' for row in rows)
    removed_finalized = sum(row['status'] == 'finalized' for row in rows)

    conn.execute('DELETE FROM assignments WHERE substr(work_date,1,7)=?' + clause, (month,))
    # Manual temporary skill transfers refer to workers' removed assignments.
    conn.executemany('DELETE FROM daily_skill_overrides WHERE work_date=? AND employee_id=?',
                     sorted(deleted_employees))

    affected_runs = 0
    has_break_runs = conn.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='cc_schedule_runs'""").fetchone() is not None
    for work_day in touched_days:
        remaining = conn.execute('SELECT status FROM assignments WHERE work_date=?',
                                 (work_day,)).fetchall()
        if remaining:
            status = ('finalized' if all(r['status'] == 'finalized' for r in remaining)
                      else 'draft')
            refresh_day(conn, work_day, status)
        else:
            conn.execute('DELETE FROM daily_skill_plans WHERE work_date=?', (work_day,))
            conn.execute('DELETE FROM daily_skill_overrides WHERE work_date=?', (work_day,))
        # Confirmed and draft break schedules depend on the old staffing.
        # Preserve their historical records while preventing stale schedules
        # from appearing as valid or confirmed in the operator's view.
        if has_break_runs:
            cur = conn.execute("""UPDATE cc_schedule_runs SET status='invalidated'
                WHERE schedule_date=? AND status IN ('draft','confirmed')""", (work_day,))
            affected_runs += cur.rowcount

    summary = (f'{month} / 仮シフト {removed_draft}件、確定シフト {removed_finalized}件を一括削除。'
               f'休憩案 {affected_runs}件を無効化。影響日 {len(touched_days)}日。')
    conn.execute("""INSERT INTO approvals(work_month,admin_username,action,reason,created_at)
        VALUES(?,?,?,?,?)""", (month, admin_username, 'シフト一括削除', summary,
                             datetime.now().isoformat(timespec='seconds')))
    # A prior first-stage approval no longer covers the now changed calendar.
    conn.execute("DELETE FROM approvals WHERE work_month=? AND action='警告確認'", (month,))
    return {'draft': removed_draft, 'finalized': removed_finalized,
            'days': len(touched_days), 'invalidated_break_runs': affected_runs}
