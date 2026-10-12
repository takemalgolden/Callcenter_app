"""New weekly-contract -> monthly target and independent monthly ceiling tests."""
import ast
import calendar
import csv
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import monthly_workdays as mw

class ContractCalculations(unittest.TestCase):
    def test_examples_five_days(self):
        self.assertEqual(mw.automatic_monthly_days('2026-10',5),22)
        self.assertEqual(mw.automatic_monthly_days('2026-11',5),21)
        self.assertEqual(mw.automatic_monthly_days('2027-02',5),20)
        self.assertEqual(mw.automatic_monthly_days('2028-02',5),21)

    def test_other_weekly_contracts(self):
        self.assertEqual(mw.automatic_monthly_days('2026-10',4),18)
        self.assertEqual(mw.automatic_monthly_days('2026-10',7),31)
        self.assertEqual(mw.automatic_monthly_days('2026-10',1),4)

    def test_manual_cap_independent(self):
        self.assertEqual(mw.monthly_target('2026-10',5,20),20)
        self.assertEqual(mw.monthly_target('2026-10',5,24),22)
        self.assertEqual(mw.monthly_target('2026-10',5,None),22)
        self.assertEqual(mw.monthly_target('2026-10',5,0),0)
        self.assertEqual(mw.monthly_target('2026-10',7,28),28)

    def test_invalid_caps_and_weeks(self):
        for cap in (-1,32):
            with self.subTest(cap=cap),self.assertRaises(ValueError):
                mw.monthly_target('2026-10',5,cap)
        for weekly in (0,8):
            with self.subTest(weekly=weekly),self.assertRaises(ValueError):
                mw.automatic_monthly_days('2026-10',weekly)

    def test_selector_cap_with_existing_finalized(self):
        days=[date(2026,10,d) for d in range(1,32)]
        candidates={d:({'id':1},10) for d in days}
        fixed=set(days[:3])
        selected,error=mw.select_workdays(days,candidates,fixed,set(),20,5,5,monthly_max=2)
        self.assertEqual(selected,fixed)
        self.assertIn('確定済み勤務',error)

    def test_selector_only_target_days_at_cap(self):
        days=[date(2026,10,d) for d in range(1,32)]
        candidates={d:({'id':1},10) for d in days}
        selected,error=mw.select_workdays(days,candidates,set(),set(),20,5,5,monthly_max=20)
        self.assertIsNone(error)
        self.assertEqual(len(selected),20)

    def test_auto_target_in_selector(self):
        days=[date(2026,10,d) for d in range(1,32)]
        candidates={d:({'id':1},10) for d in days}
        target=mw.monthly_target('2026-10',5)
        selected,error=mw.select_workdays(days,candidates,set(),set(),target,5,5)
        self.assertIsNone(error)
        self.assertEqual(len(selected),22)

    def test_old_sqlite_data_preserved_and_new_column_added(self):
        app=ROOT/'shift_planner_app/app.py'
        tree=ast.parse(app.read_text(encoding='utf-8'))
        init=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='init_db')
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'unified.sqlite3'
            c=sqlite3.connect(database)
            c.executescript('''CREATE TABLE employees (
                id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1,
                skills TEXT DEFAULT 'consumer',monthly_days INTEGER DEFAULT 20,
                weekly_max_days INTEGER DEFAULT 5,min_hours REAL DEFAULT 4,
                max_hours REAL DEFAULT 9,max_consecutive INTEGER DEFAULT 5,
                early_ok INTEGER DEFAULT 1,mid_ok INTEGER DEFAULT 1,late_ok INTEGER DEFAULT 1);
                INSERT INTO employees(id,name,monthly_days,weekly_max_days)
                VALUES (4,'以前からの登録者',19,4);
                CREATE TABLE assignments (
                id INTEGER PRIMARY KEY,employee_id INTEGER,work_date TEXT,
                pattern_id INTEGER,skill TEXT,status TEXT);
                INSERT INTO assignments VALUES (1,4,'2026-10-12',1,'consumer','finalized');''')
            c.commit();c.close()
            def db():
                con=sqlite3.connect(database);con.row_factory=sqlite3.Row
                return con
            ctx={'db':db,'generate_password_hash':lambda x:'hash',
                'DEFAULT_SHIFTS':[('早番','09:20','18:00',60)]}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[init],type_ignores=[])),str(app),'exec'),ctx)
            ctx['init_db']()
            con=db()
            row=con.execute('SELECT name,monthly_days,weekly_max_days,monthly_max_days FROM employees WHERE id=4').fetchone()
            self.assertEqual(tuple(row),('以前からの登録者',19,4,None))
            self.assertEqual(con.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],1)
            con.close()

    def test_admin_and_leave_ui_have_contract_controls(self):
        admin=(ROOT/'shift_planner_app/templates/employees.html').read_text()
        leave=(ROOT/'shift_planner_app/templates/my_requests.html').read_text()
        calendar_page=(ROOT/'shift_planner_app/templates/index.html').read_text()
        self.assertIn('name="monthly_max_days"',admin)
        self.assertIn('週最大勤務日数',admin)
        self.assertIn('targetForMonth',leave)
        self.assertIn('row.cap',calendar_page)

class ImportCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=ROOT/'shift_planner_app/app.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='parse_import_file')
        cls.env={'csv':csv,'io':io,'re':re}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(source),'exec'),cls.env)

    def decode_csv(self,header,row):
        class Upload:
            filename='users.csv'
            stream=io.BytesIO(('\ufeff'+header+'\n'+row+'\n').encode())
        return self.env['parse_import_file'](Upload())[0]

    def test_new_csv_week_and_month_cap(self):
        r=self.decode_csv('ログインID,氏名,スキル,週最大勤務日数,月間最大勤務日数',
            'employee01,田中,consumer,5,20')
        self.assertFalse(r['error'])
        self.assertEqual(r['values'][6],5)
        self.assertEqual(r['values'][7],20)
        self.assertTrue(r['values'][8])

    def test_old_csv_monthly_target_ignored(self):
        r=self.decode_csv('ログインID,氏名,スキル,月間勤務日数,週最大勤務日数',
            'employee01,田中,consumer,14,4')
        self.assertFalse(r['error'])
        self.assertEqual(r['values'][6],4)
        self.assertIsNone(r['values'][7])
        self.assertFalse(r['values'][8])

    def test_invalid_monthly_cap_rejected(self):
        r=self.decode_csv('ログインID,氏名,スキル,月間最大勤務日数',
            'employee01,田中,PP,33')
        self.assertTrue(r['error'])


if __name__=='__main__': unittest.main()
