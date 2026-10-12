"""One-time explicit import from old break SQLite to canonical unified employees/users.
Usage: python import_existing_break_users.py path/to/old/callcenter.sqlite3
No passwords imported: admin resets temporary passwords in unified UI.
"""
import sys, sqlite3, os, secrets
from pathlib import Path
from werkzeug.security import generate_password_hash
ROOT=Path(__file__).resolve().parent
if len(sys.argv)!=2: raise SystemExit('使い方: python import_existing_break_users.py 旧休憩管理DBのパス')
src=Path(sys.argv[1]).resolve(); dest=ROOT/'data'/'unified.sqlite3'
if not src.is_file() or not dest.is_file() or src==dest: raise SystemExit('旧DBと統合DBの場所を確認してください。統合アプリを一度起動してから実行してください。')
with sqlite3.connect(f'file:{src.as_posix()}?mode=ro',uri=True) as old:
    old.row_factory=sqlite3.Row
    users=old.execute("SELECT username,display_name,skills,active FROM users WHERE role='operator'").fetchall()
with sqlite3.connect(dest) as c:
    c.execute('PRAGMA busy_timeout=10000')
    added=skipped=0
    for u in users:
        if c.execute('SELECT 1 FROM users WHERE username=?',(u['username'],)).fetchone():
            skipped+=1;continue
        import json
        skills=json.loads(u['skills'] or '[]')
        skills=','.join('PP' if x.upper()=='PP' else x.lower() for x in skills if x.lower() in ('consumer','merchant','pp')) or 'consumer'
        eid=c.execute('INSERT INTO employees(name,skills,active) VALUES(?,?,?)',(u['display_name'],skills,int(u['active']))).lastrowid
        c.execute('INSERT INTO users(username,password_hash,role,employee_id,must_change_password,approved) VALUES(?,?,\'operator\',?,1,1)',(u['username'],generate_password_hash(secrets.token_urlsafe(30)),eid))
        added+=1
print(f'取込完了: {added}件 / 重複スキップ: {skipped}件。管理者画面から各ユーザーのPWを再設定してください。')
