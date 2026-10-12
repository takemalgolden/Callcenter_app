"""Import legacy break-management-only operators into the canonical employee list.

Runs once per missing operator on startup; re-runs are safe.  Nothing is deleted.
The old break-side credentials are not copied to the canonical login;
the administrator must set a new password in shared employee management.
"""
import json
import secrets
import sqlite3

SKILL_MAP = {"Consumer": "consumer", "Merchant": "merchant", "PP": "PP"}


def migrate_break_only_operators(connection):
    """Return number of recovered legacy operator accounts.

    Excludes untouched demo placeholders created by a blank break database.
    Existing canonical employee/account state always takes precedence.
    """
    connection.row_factory = sqlite3.Row
    tables = {r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {'users', 'employees', 'cc_users'}.issubset(tables):
        return 0
    legacy = connection.execute("""SELECT id, username, display_name, skills, active
                                   FROM cc_users WHERE role='operator' ORDER BY id""").fetchall()
    recovered = 0
    for row in legacy:
        username = row['username']
        if connection.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            continue
        # Never convert untouched break-engine demonstration users into employees.
        sample_num = username.removeprefix('operator')
        if (sample_num in {'1','2','3','4','5','6'}
                and row['display_name'] == 'Operator ' + sample_num):
            used = connection.execute('SELECT 1 FROM cc_attendance WHERE user_id=? LIMIT 1', (row['id'],)).fetchone() if 'cc_attendance' in tables else None
            modified = connection.execute('SELECT 1 FROM cc_shift_overrides WHERE user_id=? LIMIT 1', (row['id'],)).fetchone() if 'cc_shift_overrides' in tables else None
            if not used and not modified:
                continue
        # The employee may exist already under its original break account ID.
        employee = connection.execute('SELECT id FROM employees WHERE source_user_id=?', (row['id'],)).fetchone()
        if employee is not None:
            eid = employee['id']
            # If another canonical login is already bound, keep that identity.
            if connection.execute('SELECT 1 FROM users WHERE employee_id=?', (eid,)).fetchone():
                continue
        else:
            try:
                raw_skills = json.loads(row['skills'] or '[]')
            except (TypeError, ValueError):
                raw_skills = []
            skills = ','.join(dict.fromkeys(SKILL_MAP[s] for s in raw_skills if s in SKILL_MAP)) or 'consumer'
            cur = connection.execute("""INSERT INTO employees
                (name,skills,active,source_user_id,source_username)
                VALUES(?,?,?,?,?)""",
                (row['display_name'],skills,int(bool(row['active'])),row['id'],username))
            eid = cur.lastrowid
        # Unusable local placeholder; the administrator must reset it before login.
        # The original PBKDF2 credentials remain untouched in cc_users.
        placeholder = '!legacy-' + secrets.token_urlsafe(32)
        connection.execute("""INSERT INTO users
            (username,password_hash,role,employee_id,approved,must_change_password)
            VALUES(?,?,'operator',?,1,1)""", (username,placeholder,eid))
        recovered += 1
    return recovered
