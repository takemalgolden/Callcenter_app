"""Sync break attendance with finalized shifts in the shared SQLite database.

Shift finalization is the authoritative *planned* roster. Day-of manual attendance
changes remain authoritative after that finalization until a new finalization.
This module never modifies assignments, saved manual work hours, or break plans.
"""
from datetime import date, datetime, timedelta
import re


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _days(month):
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month or ''):
        raise ValueError('対象月の形式が不正です。')
    first = date.fromisoformat(month + '-01')
    after = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    day = first
    while day < after:
        yield day.isoformat()
        day += timedelta(days=1)


def _roster(con, month):
    """Only active, approved operator accounts participate in break management."""
    accounts = con.execute("""SELECT cc.id AS cc_id, su.employee_id, cc.username
        FROM cc_users cc
        JOIN users su ON su.username=cc.username AND su.role IN ('operator','admin')
        JOIN employees e ON e.id=su.employee_id AND e.active=1
        WHERE cc.role IN ('operator','admin') AND cc.active=1 AND su.approved=1
        ORDER BY cc.id""").fetchall()
    by_employee = {}
    for r in accounts:
        by_employee.setdefault(r['employee_id'], []).append(r['cc_id'])
    # Cannot silently exclude an employee whose finalized assignment lacks
    # a matching/active break-management account.
    unmatched = con.execute("""SELECT DISTINCT a.employee_id,e.name
        FROM assignments a JOIN employees e ON e.id=a.employee_id
        WHERE substr(a.work_date,1,7)=? AND a.status='finalized'
        AND e.active=1""", (month,)).fetchall()
    missing = [r['name'] for r in unmatched if r['employee_id'] not in by_employee]
    if missing:
        raise ValueError('休憩管理へのオペレーター同期が未完了です: '+', '.join(missing[:10]))
    return accounts


def _target_for_day(con, day, accounts):
    assigned = {r[0] for r in con.execute("""SELECT employee_id FROM assignments
        WHERE work_date=? AND status='finalized'""", (day,))}
    # An authorized on-the-day change can create an additional, temporary shift.
    extras = {r[0] for r in con.execute("""SELECT user_id FROM cc_shift_overrides
        WHERE work_date=? AND shift_start<>'' AND shift_end<>''""", (day,))}
    return {r['cc_id']: int(r['employee_id'] in assigned or r['cc_id'] in extras)
            for r in accounts}


def _upsert(con, day, cc_id, present, stamp, actor_id):
    con.execute("""INSERT INTO cc_attendance
       (attendance_date,user_id,present,updated_at,updated_by)
       VALUES(?,?,?,?,?) ON CONFLICT(attendance_date,user_id) DO UPDATE SET
       present=excluded.present,updated_at=excluded.updated_at,
       updated_by=excluded.updated_by""", (day,cc_id,present,stamp,actor_id))


def sync_month(con, month, actor_username=None):
    """Force-sync the planned roster in the same transaction as month finalization.

    Clears stale manually unchecked values from BEFORE this finalization.
    Subsequent manual attendance changes remain effective until next finalize.
    Caller owns transaction and commit/rollback.
    """
    needed = {'cc_users','cc_attendance','cc_shift_overrides',
              'assignments','employees','users'}
    if not needed.issubset(_tables(con)):
        # Standalone shift-planner is still supported and has no break tables.
        return {'available':False,'days':0,'present':0,'off':0,'operators':0}
    accounts = _roster(con,month)
    admin = con.execute("SELECT id FROM cc_users WHERE username=? AND role='admin'",
                        (actor_username,)).fetchone() if actor_username else None
    actor_id = admin[0] if admin else None
    stamp = datetime.now().isoformat(timespec='seconds')
    present_count, off_count, days = 0, 0, 0
    for day in _days(month):
        planned = _target_for_day(con, day, accounts)
        for uid, present in planned.items():
            _upsert(con,day,uid,present,stamp,actor_id)
            if present: present_count += 1
            else: off_count += 1
        days += 1
    return {'available':True,'days':days,'present':present_count,
            'off':off_count,'operators':len(accounts)}


def repair_day_if_older_than_finalization(con, day):
    """Repair old databases without overriding manual changes after finalization.

    Only attendance records last touched no later than the finalization are
    overwritten. The caller must commit. No-op for standalone break management.
    """
    needed={'cc_users','cc_attendance','cc_shift_overrides','assignments',
            'employees','users','approvals'}
    if not needed.issubset(_tables(con)):
        return 0
    try:
        date.fromisoformat(day)
        if len(day)!=10: return 0
    except ValueError:
        return 0
    row=con.execute("""SELECT created_at FROM approvals
        WHERE work_month=? AND action='最終確定'
        ORDER BY id DESC LIMIT 1""", (day[:7],)).fetchone()
    if not row: return 0
    stamp = row[0]
    try:
        accounts = _roster(con,day[:7])
    except ValueError:
        # Historical databases may have orphaned assignments. Leave attendance
        # untouched instead of breaking the entire break-management screen;
        # finalization performs the strict, administrator-visible validation.
        return 0
    planned = _target_for_day(con,day,accounts)
    saved = {r['user_id']:r for r in con.execute("""SELECT user_id,updated_at
            FROM cc_attendance WHERE attendance_date=?""",(day,))}
    touched = 0
    now=datetime.now().isoformat(timespec='seconds')
    for uid, present in planned.items():
        old=saved.get(uid)
        # A manual change after finalization is intentional: never undo it.
        if old and old['updated_at'] > stamp:
            continue
        _upsert(con,day,uid,present,now,None)
        touched += 1
    return touched
