"""Regression checks for legacy disabled break operator migration and UI consolidation."""
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('legacy_operator_migration', ROOT/'legacy_operator_migration.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_path=Path(self.tmp.name)/'test.sqlite3'
        self.con=sqlite3.connect(self.db_path)
        self.con.row_factory=sqlite3.Row
        self.con.executescript('''
            CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, skills TEXT, active INTEGER,
               source_user_id INTEGER, source_username TEXT);
            CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE, password_hash TEXT,
               role TEXT,employee_id INTEGER,approved INTEGER,must_change_password INTEGER);
            CREATE TABLE cc_users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,
               role TEXT,skills TEXT,active INTEGER);
            CREATE TABLE cc_attendance(attendance_date TEXT,user_id INTEGER,present INTEGER);
            CREATE TABLE cc_shift_overrides(work_date TEXT,user_id INTEGER);
            INSERT INTO users(username,password_hash,role,approved) VALUES('admin','hash','admin',1);
            INSERT INTO cc_users(id,username,display_name,role,skills,active)
               VALUES(41,'disabled_old','旧休憩登録者','operator','["Consumer","PP"]',0),
                     (42,'active_old','旧稼働者','operator','["Merchant"]',1),
                     (43,'operator1','Operator 1','operator','["Consumer"]',0);
        ''')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_disabled_legacy_account_is_recovered_without_reactivation(self):
        self.assertEqual(mod.migrate_break_only_operators(self.con),2)
        rows=self.con.execute('SELECT e.*,u.username,u.approved FROM employees e JOIN users u ON u.employee_id=e.id ORDER BY e.name').fetchall()
        by_name={x['name']:x for x in rows}
        self.assertEqual(by_name['旧休憩登録者']['active'],0)
        self.assertEqual(by_name['旧休憩登録者']['source_user_id'],41)
        self.assertEqual(by_name['旧休憩登録者']['skills'],'consumer,PP')
        self.assertEqual(by_name['旧稼働者']['active'],1)
        self.assertNotIn('Operator 1',by_name)
        self.assertEqual(mod.migrate_break_only_operators(self.con),0)
        self.assertEqual(self.con.execute('SELECT count(*) FROM employees').fetchone()[0],2)

    def test_restored_employee_is_kept_and_login_can_be_approved(self):
        mod.migrate_break_only_operators(self.con)
        e=self.con.execute("SELECT id FROM employees WHERE source_user_id=41").fetchone()
        self.con.execute('UPDATE employees SET active=1 WHERE id=?',(e['id'],))
        self.con.execute('UPDATE users SET approved=1 WHERE employee_id=?',(e['id'],))
        self.con.commit()
        self.assertEqual(mod.migrate_break_only_operators(self.con),0)
        state=self.con.execute('SELECT active FROM employees WHERE id=?',(e['id'],)).fetchone()['active']
        self.assertEqual(state,1)

    def test_new_canonical_profile_is_not_overwritten_by_old_break_state(self):
        self.con.execute('INSERT INTO employees(id,name,skills,active,source_user_id,source_username) VALUES(7,?,?,?,?,?)',('社員','consumer',1,41,'disabled_old'))
        self.con.execute("INSERT INTO users(username,password_hash,role,employee_id,approved) VALUES('disabled_old','x','operator',7,1)")
        self.assertEqual(mod.migrate_break_only_operators(self.con),1)
        self.assertEqual(self.con.execute('SELECT active FROM employees WHERE id=7').fetchone()[0],1)


    def test_break_attendance_only_returns_active_canonical_employees(self):
        # The daily rest planner must never surface disabled break-side users,
        # even when attendance history exists for them.
        import os, sys
        sys.path.insert(0, str(ROOT/'break_engine'))
        import server as break_server
        old_db=break_server.DB_PATH
        try:
            break_server.DB_PATH=str(Path(self.tmp.name)/'rest.sqlite3')
            break_server.init_db()
            with sqlite3.connect(break_server.DB_PATH) as rest:
                rest.row_factory=sqlite3.Row
                rest.executescript('''
                    CREATE TABLE employees(id INTEGER PRIMARY KEY,name TEXT,active INTEGER);
                    CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,employee_id INTEGER,role TEXT,approved INTEGER);
                    INSERT INTO employees VALUES(1,'A',1),(2,'B',0);
                    INSERT INTO users VALUES(1,'worker_a',1,'operator',1),(2,'worker_b',2,'operator',1);
                ''')
                rest.execute("INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,active) VALUES(?,?,?,?,?,?,?)",
                    ('worker_a','A','operator','00','00','["Consumer"]',1))
                rest.execute("INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,active) VALUES(?,?,?,?,?,?,?)",
                    ('worker_b','B','operator','00','00','["Merchant"]',0))
                rest.commit()
                people=break_server.dated_operators(rest,'2026-11-04')
                self.assertEqual([p['username'] for p in people], ['worker_a'])
                rest.execute('UPDATE employees SET active=1 WHERE id=2')
                rest.execute("UPDATE cc_users SET active=1 WHERE username='worker_b'")
                rest.commit()
                people=break_server.dated_operators(rest,'2026-11-04')
                self.assertEqual({p['username'] for p in people}, {'worker_a','worker_b'})
        finally:
            break_server.DB_PATH=old_db

    def test_break_editor_removed_and_employee_reactivation_visible(self):
        html=(ROOT/'break_engine/static/index.html').read_text(encoding='utf-8')
        employee_html=(ROOT/'shift_planner_app/templates/employees.html').read_text(encoding='utf-8')
        br=(ROOT/'break_engine/server.py').read_text(encoding='utf-8')
        self.assertNotIn('id="userForm"',html)
        self.assertNotIn('id="tab-people"',html)
        self.assertNotIn('loadUsers()',html)
        self.assertIn('href="/employees"',html)
        self.assertIn('有効化する',employee_html)
        self.assertIn('WHERE active=1 ORDER BY role DESC,display_name',br)

if __name__=='__main__':unittest.main()
