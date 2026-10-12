"""Shared-DB attendance reconciliation: no Flask/PyInstaller prerequisite."""
import ast
from datetime import datetime
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('attendance_sync_test', ROOT / 'shift_planner_app' / 'attendance_sync.py')
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class AttendanceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = sqlite3.connect(str(Path(self.tmp.name) / 'test.sqlite3'))
        self.con.row_factory = sqlite3.Row
        self.con.executescript('''
           CREATE TABLE employees (id INTEGER PRIMARY KEY,name TEXT,active INTEGER);
           CREATE TABLE users (id INTEGER PRIMARY KEY,username TEXT,role TEXT,employee_id INTEGER,approved INTEGER);
           CREATE TABLE cc_users(id INTEGER PRIMARY KEY,username TEXT,role TEXT,active INTEGER);
           CREATE TABLE assignments(employee_id INTEGER,work_date TEXT,status TEXT);
           CREATE TABLE approvals(id INTEGER PRIMARY KEY,work_month TEXT,admin_username TEXT,action TEXT,reason TEXT,created_at TEXT);
           CREATE TABLE cc_attendance(attendance_date TEXT,user_id INTEGER,present INTEGER,
                 updated_at TEXT, updated_by INTEGER,PRIMARY KEY(attendance_date,user_id));
           CREATE TABLE cc_shift_overrides(work_date TEXT,user_id INTEGER,shift_start TEXT,shift_end TEXT);
           INSERT INTO employees VALUES(1,'田中',1),(2,'佐藤',1),(3,'鈴木',1);
           INSERT INTO users VALUES(1,'admin','admin',NULL,1),(2,'a','operator',1,1),
                                  (3,'b','operator',2,1),(4,'c','operator',3,1);
           INSERT INTO cc_users VALUES(1,'admin','admin',1),(11,'a','operator',1),
                                       (12,'b','operator',1),(13,'c','operator',1);
           INSERT INTO assignments VALUES(1,'2026-11-05','finalized'),
                                         (2,'2026-11-05','finalized'),
                                         (1,'2026-11-06','finalized');
           INSERT INTO cc_attendance VALUES('2026-11-05',11,0,'2026-11-03T08:00:00',1),
                                           ('2026-11-05',12,0,'2026-11-03T08:00:00',1),
                                           ('2026-11-05',13,1,'2026-11-03T08:00:00',1);
        ''')
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def state(self, day):
        return {x['user_id']:x['present'] for x in self.con.execute(
            'SELECT user_id,present FROM cc_attendance WHERE attendance_date=?',(day,))}

    def test_monthly_finalization_turns_only_assigned_operators_on(self):
        report=sync.sync_month(self.con,'2026-11','admin')
        self.con.commit()
        self.assertEqual(report,{'available':True,'days':30,'present':3,'off':87,'operators':3})
        self.assertEqual(self.state('2026-11-05'),{11:1,12:1,13:0})
        self.assertEqual(self.state('2026-11-06'),{11:1,12:0,13:0})
        self.assertEqual(self.state('2026-11-07'),{11:0,12:0,13:0})

    def test_manual_absence_after_confirmation_not_undone_by_read(self):
        self.con.execute("INSERT INTO approvals(id,work_month,action,created_at) VALUES(1,'2026-11','最終確定','2026-11-01T12:00:00')")
        sync.sync_month(self.con,'2026-11','admin')
        self.con.execute("UPDATE cc_attendance SET present=0,updated_at='2026-11-30T09:00:00' WHERE attendance_date='2026-11-05' AND user_id=11")
        self.con.commit()
        sync.repair_day_if_older_than_finalization(self.con,'2026-11-05')
        self.con.commit()
        self.assertEqual(self.state('2026-11-05')[11],0)
        self.assertEqual(self.state('2026-11-05')[12],1)

    def test_existing_pre_finalization_unchecked_rows_recovered(self):
        self.con.execute("INSERT INTO approvals(id,work_month,action,created_at) VALUES(1,'2026-11','最終確定','2026-11-04T12:00:00')")
        self.assertGreater(sync.repair_day_if_older_than_finalization(self.con,'2026-11-05'),0)
        self.con.commit()
        self.assertEqual(self.state('2026-11-05'),{11:1,12:1,13:0})

    def test_work_hours_override_sets_adhoc_day_present(self):
        self.con.execute("INSERT INTO cc_shift_overrides VALUES('2026-11-07',13,'09:20','18:00')")
        sync.sync_month(self.con,'2026-11')
        self.con.commit()
        self.assertEqual(self.state('2026-11-07'),{11:0,12:0,13:1})

    def test_new_operator_not_silently_skipped(self):
        self.con.execute("UPDATE cc_users SET active=0 WHERE username='b'")
        with self.assertRaisesRegex(ValueError,'佐藤'):
            sync.sync_month(self.con,'2026-11')

    def test_inactive_employee_and_unapproved_account_not_checked(self):
        self.con.execute('UPDATE employees SET active=0 WHERE id=3')
        self.con.execute('UPDATE users SET approved=0 WHERE username=\'b\'')
        self.con.execute("DELETE FROM assignments WHERE employee_id=2")
        rep=sync.sync_month(self.con,'2026-11')
        self.assertEqual(rep['operators'],1)
        self.assertEqual(self.state('2026-11-05')[11],1)
        # Old legacy records for inactive employees are not implicitly deleted.
        self.assertEqual(self.state('2026-11-05')[12],0)

    def test_standalone_shift_app_has_no_break_tables(self):
        with sqlite3.connect(':memory:') as c:
            self.assertFalse(sync.sync_month(c,'2026-11')['available'])
            self.assertEqual(sync.repair_day_if_older_than_finalization(c,'2026-11-05'),0)

    def test_finalization_transaction_rolls_back_attendance_and_shift(self):
        # Mirrors the enclosing `with db() as c` finalization transaction.
        with self.assertRaises(RuntimeError):
            with sqlite3.connect(str(Path(self.tmp.name)/'test.sqlite3')) as c:
                c.row_factory=sqlite3.Row
                c.execute('UPDATE assignments SET status=\'draft\'')
                sync.sync_month(c,'2026-11')
                raise RuntimeError('simulated commit failure')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignments WHERE status='finalized'").fetchone()[0],3)
        self.assertEqual(self.state('2026-11-05')[11],0)

    def test_admin_privileged_operator_is_included_in_roster(self):
        self.con.execute("UPDATE users SET role='admin' WHERE username='b'")
        self.con.execute("UPDATE cc_users SET role='admin' WHERE username='b'")
        sync.sync_month(self.con,'2026-11','admin')
        self.con.commit()
        self.assertEqual(self.state('2026-11-05'),{11:1,12:1,13:0})

    def test_finalization_route_saves_attendance_atomically(self):
        # Run the real Flask route body with a minimal request/session shim.
        # This verifies the new sync call happens during finalization and
        # changes stale attendance in the committed database, not just in a unit.
        import calendar
        from datetime import date
        tree=ast.parse((ROOT/'shift_planner_app/app.py').read_text('utf-8'))
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='finalize')
        fn.decorator_list=[]
        month='2026-11'
        self.con.execute("INSERT INTO approvals(id,work_month,action,created_at) VALUES(1,?,'警告確認','2026-11-03T07:00:00')",(month,))
        self.con.execute("UPDATE assignments SET status='draft'")
        self.con.commit()
        class Form:
            form={'month':month,'stage':'final','reason':'勤務調整のため'}
        flashed=[]
        path=Path(self.tmp.name)/'test.sqlite3'
        def database():
            c=sqlite3.connect(str(path))
            c.row_factory=sqlite3.Row
            return c
        ctx={'db':database,'request':Form,'session':{'username':'admin'},
             'validate_month':lambda m:[],'datetime':datetime,'sqlite3':sqlite3,
             'month_days':lambda m:[date.fromisoformat(m+'-05')],
             '_refresh_day_plan':lambda *a:None,
             'attendance_sync':sync,'flash':lambda *args:flashed.append(args),
             'redirect':lambda url:url,'url_for':lambda endpoint,**k:endpoint+':'+k.get('modal','')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),str(ROOT/'shift_planner_app/app.py'),'exec'),ctx)
        self.assertEqual(ctx['finalize'](),'index:result')
        self.assertIn('出勤状態も30日分同期',flashed[-1][0])
        self.assertEqual(self.state('2026-11-05'),{11:1,12:1,13:0})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignments WHERE status='finalized'").fetchone()[0],3)

    def test_finalization_does_not_commit_if_roster_unmapped(self):
        from datetime import date
        tree=ast.parse((ROOT/'shift_planner_app/app.py').read_text('utf-8'))
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='finalize')
        fn.decorator_list=[]
        self.con.execute("INSERT INTO approvals(id,work_month,action,created_at) VALUES(1,'2026-11','警告確認','2026-11-03T07:00:00')")
        self.con.execute("UPDATE assignments SET status='draft'")
        self.con.execute("DELETE FROM cc_users WHERE username='b'")
        self.con.commit()
        class Form:
            form={'month':'2026-11','stage':'final','reason':'勤務調整のため'}
        messages=[]
        path=Path(self.tmp.name)/'test.sqlite3'
        def database():
            c=sqlite3.connect(str(path))
            c.row_factory=sqlite3.Row
            return c
        ctx={'db':database,'request':Form,'session':{'username':'admin'},
             'validate_month':lambda m:[],'datetime':datetime,'sqlite3':sqlite3,
             'month_days':lambda m:[date(2026,11,5)],
             '_refresh_day_plan':lambda *a:None,
             'attendance_sync':sync,'flash':lambda *args:messages.append(args),
             'redirect':lambda url:url,'url_for':lambda endpoint,**k:endpoint+':'+k.get('modal','')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),str(ROOT/'shift_planner_app/app.py'),'exec'),ctx)
        self.assertEqual(ctx['finalize'](),'index:warnings')
        self.assertTrue(any('佐藤' in m[0] for m in messages))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignments WHERE status='finalized'").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM approvals WHERE action='最終確定'").fetchone()[0],0)

    def test_break_engine_reads_finalized_attendance_including_admin_employee(self):
        spec=importlib.util.spec_from_file_location('attendance_break_reader',ROOT/'break_engine/server.py')
        br=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)
        before=br.DB_PATH
        br.DB_PATH=str(Path(self.tmp.name)/'test.sqlite3')
        try:
            # Minimal display columns needed by as_user. Account 12 is an
            # operator who also has admin rights, not a second admin person.
            for column,definition in (
                ('display_name','TEXT'),('skills','TEXT'),('shift_start','TEXT'),('shift_end','TEXT')):
                self.con.execute(f'ALTER TABLE cc_users ADD COLUMN {column} {definition}')
            self.con.execute("UPDATE cc_users SET display_name=username, skills='[\"Consumer\"]',shift_start='09:20',shift_end='18:00'")
            self.con.execute("UPDATE users SET role='admin' WHERE username='b'")
            self.con.execute("UPDATE cc_users SET role='admin' WHERE username='b'")
            self.con.execute("CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY,start_time TEXT,end_time TEXT,break_minutes INTEGER)")
            self.con.execute("INSERT INTO shift_patterns VALUES(7,'09:20','18:00',60)")
            self.con.execute("ALTER TABLE assignments ADD COLUMN pattern_id INTEGER DEFAULT 7")
            self.con.execute("ALTER TABLE assignments ADD COLUMN skill TEXT DEFAULT 'consumer'")
            self.con.execute("INSERT INTO approvals(id,work_month,action,created_at) VALUES(9,'2026-11','最終確定','2026-11-04T00:00:00')")
            sync.sync_month(self.con,'2026-11','admin')
            self.con.commit()
            entries=br.dated_operators(self.con,'2026-11-05')
            self.assertEqual({r['id']:r['present'] for r in entries},{11:True,12:True,13:False})
            self.assertEqual(next(r for r in entries if r['id']==12)['source'],'finalized')
        finally:
            br.DB_PATH=before

    def test_integrations_wired_to_finalization_and_break_api(self):
        shift=(ROOT/'shift_planner_app/app.py').read_text('utf-8')
        unified=(ROOT/'unified.py').read_text('utf-8')
        self.assertIn('attendance_sync.sync_month(c, month, session["username"])',shift)
        self.assertIn('shift.attendance_sync.repair_day_if_older_than_finalization(c,day)',unified)
        self.assertIn("request.path=='/finalize' and request.method=='POST'",unified)

if __name__ == '__main__': unittest.main()
