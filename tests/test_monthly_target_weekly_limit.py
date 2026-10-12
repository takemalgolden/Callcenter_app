"""Tests of mandatory monthly days and per-Sunday-through-Saturday-week maximum (stdlib-only)."""
import ast
import calendar
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date,datetime,timedelta
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import monthly_workdays
import flexible_optimizer as flex


def days_for(y,m):
    return [date(y,m,n) for n in range(1,calendar.monthrange(y,m)[1]+1)]


def weeks(dates):
    result={}
    for day in dates:
        sunday=day-timedelta(days=(day.weekday()+1)%7)
        result[sunday]=result.get(sunday,0)+1
    return result


class WorkingDaysTests(unittest.TestCase):
    def setUp(self):
        self.days=days_for(2026,11)
        self.options={d:({'id':1},10) for d in self.days}

    def test_20_days_and_maximum_five_per_week(self):
        chosen,error=monthly_workdays.select_workdays(self.days,self.options,set(),set(),20,5,5)
        self.assertIsNone(error)
        self.assertEqual(len(chosen),20)
        self.assertLessEqual(max(weeks(chosen).values()),5)

    def test_approved_time_off_excluded_but_target_kept(self):
        blocked={date(2026,11,d) for d in (2,3,4)}
        chosen,error=monthly_workdays.select_workdays(self.days,{
            d:choice for d,choice in self.options.items() if d not in blocked},set(),set(),20,5,5)
        self.assertIsNone(error)
        self.assertEqual(len(chosen),20)
        self.assertFalse(chosen & blocked)

    def test_cannot_hit_monthly_target_does_not_fake_success(self):
        chosen,error=monthly_workdays.select_workdays(self.days,self.options,set(),set(),25,5,5)
        self.assertIsNotNone(error)
        self.assertLess(len(chosen),25)
        self.assertIn('登録25日',error)

    def test_preserves_finalized_work_and_respects_external_week(self):
        past={date(2026,10,30),date(2026,10,31)}
        fixed={date(2026,11,2),date(2026,11,3)}
        chosen,error=monthly_workdays.select_workdays(self.days,self.options,fixed,past,9,2,5)
        self.assertIsNone(error)
        self.assertEqual(len(chosen),9)
        self.assertTrue(fixed <= chosen)
        # 2026-11-01 starts a new Sunday-Saturday week.
        self.assertTrue(all(n<=2 for n in weeks(chosen|past).values()))

    def test_fixed_week_over_limit_is_error(self):
        fixed={date(2026,11,d) for d in (2,3,4)}
        _,error=monthly_workdays.select_workdays(self.days,self.options,fixed,set(),15,2,5)
        self.assertIsNotNone(error)
        self.assertIn('矛盾',error)

    def test_next_month_consecutive_work_blocks_end_of_month(self):
        suffix={date(2026,12,d) for d in (1,2,3,4,5)}
        chosen,error=monthly_workdays.select_workdays(self.days,self.options,set(),suffix,20,5,5)
        self.assertIsNone(error)
        self.assertNotIn(date(2026,11,30),chosen)


SOURCE=ROOT/'shift_planner_app/app.py'
TREE=ast.parse(SOURCE.read_text('utf-8'))
NEEDED={'month_days','mins','_hours_as_minutes','_pattern_total_minutes',
        '_pattern_shift_allowed','_pattern_hours_match','_available_candidates',
        '_half_day_pattern_match','_day_candidate_patterns','_request_penalty','generate'}
funcs=[]
for node in TREE.body:
    if isinstance(node,ast.FunctionDef) and node.name in NEEDED:
        node.decorator_list=[]
        funcs.append(node)
    elif isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in
            {'FULL_DAY_LEAVE_TYPES','HALF_DAY_LEAVE_TYPES','HALF_DAY_SPLIT'} for t in node.targets):
        funcs.append(node)

class Req:
    form={}

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'demo.db'
        c=sqlite3.connect(self.path)
        c.executescript('''
        CREATE TABLE employees(id INTEGER PRIMARY KEY,name TEXT,active INTEGER,skills TEXT,monthly_days INTEGER,
            weekly_max_days INTEGER,monthly_max_days INTEGER,max_consecutive INTEGER,min_hours REAL,max_hours REAL,
            early_ok INTEGER,mid_ok INTEGER,late_ok INTEGER);
        CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY,name TEXT,start_time TEXT,end_time TEXT,
            break_minutes INTEGER,active INTEGER);
        CREATE TABLE requests(id INTEGER PRIMARY KEY,employee_id INTEGER,request_date TEXT,request_type TEXT,status TEXT);
        CREATE TABLE assignments(id INTEGER PRIMARY KEY,employee_id INTEGER,work_date TEXT,pattern_id INTEGER,
            skill TEXT,status TEXT, UNIQUE(employee_id,work_date));
        CREATE TABLE coverage(id INTEGER PRIMARY KEY,weekday INTEGER,skill TEXT,start_time TEXT,end_time TEXT,min_count INTEGER);
        INSERT INTO shift_patterns VALUES(1,'早番','09:20','18:00',60,1);
        INSERT INTO employees VALUES(1,'佐藤',1,'consumer',20,5,NULL,5,8,9,1,1,1);
        ''')
        c.commit();c.close()
        self.flashes=[]
        self.context={
            'db':self.db,'month_days':days_for, 'monthly_workdays':monthly_workdays,
            'sqlite3':sqlite3,'date':date,'datetime':datetime,'timedelta':timedelta,
            'calendar':calendar,'Decimal':Decimal,'InvalidOperation':InvalidOperation,'ROUND_HALF_UP':ROUND_HALF_UP,
            'flex':flex,'request':Req,'flash':lambda *args:self.flashes.append(args),
            'redirect':lambda target:('redirect',target),'url_for':lambda name,**k:name+'?'+str(k),
            'shift_generation_readiness':lambda employees,patterns:{'reasons':[]},
            '_refresh_day_plan':lambda *args:{'shortages':[],'skill_switches':0},
        }
        # month_days from app.py, not our days_for signature
        self.context.pop('month_days')
        exec(compile(ast.fix_missing_locations(ast.Module(body=funcs,type_ignores=[])),str(SOURCE),'exec'),self.context)
        Req.form={'month':'2026-11'}

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        c=sqlite3.connect(self.path)
        c.row_factory=sqlite3.Row
        return c

    def rows(self):
        with self.db() as c:
            return [dict(x) for x in c.execute('SELECT * FROM assignments ORDER BY work_date')]

    def test_complete_month_keeps_approved_leave_off(self):
        with self.db() as c:
            for d in [2,3,4]:
                c.execute("INSERT INTO requests(employee_id,request_date,request_type,status) VALUES(1,?,'通常の休み希望','approved')",(f'2026-11-{d:02d}',))
        self.context['generate']()
        rows=self.rows()
        self.assertEqual(len(rows),21)
        self.assertEqual({r['status'] for r in rows},{'draft'})
        self.assertFalse({r['work_date'] for r in rows} & {'2026-11-02','2026-11-03','2026-11-04'})
        self.assertTrue(all(n<=5 for n in weeks([date.fromisoformat(r['work_date']) for r in rows]).values()))

    def test_infeasible_month_does_not_erase_previous_draft(self):
        with self.db() as c:
            c.execute('UPDATE employees SET monthly_max_days=30')
            for d in range(6, 30):
                c.execute("INSERT INTO requests(employee_id,request_date,request_type,status) VALUES(1,?,'確定休','approved')", (f'2026-11-{d:02d}',))
            c.execute("INSERT INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(1,'2026-11-01',1,'consumer','draft')")
        original=self.rows()
        self.context['generate']()
        self.assertEqual(self.rows(),original)
        self.assertTrue(any('変更していません' in item[0] for item in self.flashes))

    def test_preserves_fixed_and_external_week(self):
        with self.db() as c:
            c.execute('UPDATE employees SET weekly_max_days=2')
            for date_ in ('2026-10-30','2026-10-31','2026-11-02'):
                c.execute('INSERT INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(1,?,1,?,\'finalized\')',(date_,'consumer'))
        self.context['generate']()
        november=[r for r in self.rows() if r['work_date'].startswith('2026-11')]
        self.assertEqual(len(november),9)
        self.assertEqual([r['status'] for r in november if r['work_date']=='2026-11-02'],['finalized'])
        self.assertTrue(all(n<=2 for n in weeks([date.fromisoformat(r['work_date']) for r in self.rows()]).values()))

if __name__=='__main__':unittest.main()

class MigrationTests(unittest.TestCase):
    def test_existing_database_adds_weekly_cap_preserving_data(self):
        with tempfile.TemporaryDirectory() as directory:
            filename=Path(directory)/'old.sqlite3'
            c=sqlite3.connect(filename)
            c.executescript('''CREATE TABLE employees(
                id INTEGER PRIMARY KEY, name TEXT,active INTEGER DEFAULT 1,skills TEXT DEFAULT 'consumer',
                monthly_days INTEGER DEFAULT 20,min_hours REAL DEFAULT 4,max_hours REAL DEFAULT 9,
                max_consecutive INTEGER DEFAULT 5,early_ok INTEGER DEFAULT 1,
                mid_ok INTEGER DEFAULT 1,late_ok INTEGER DEFAULT 1);
                INSERT INTO employees(id,name,monthly_days) VALUES (2,'既存の登録者',21);''')
            c.commit();c.close()
            context={
                'db':lambda:sqlite3.connect(filename),
                'generate_password_hash':lambda password:'hash',
                'DEFAULT_SHIFTS':[('早番','09:20','18:00',60)],
            }
            # Execute exactly the init_db implementation, without importing Flask.
            node=next(node for node in TREE.body if isinstance(node,ast.FunctionDef) and node.name=='init_db')
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),
                str(SOURCE),'exec'),context)
            context['init_db']()
            c=sqlite3.connect(filename)
            self.assertEqual(c.execute('SELECT name,monthly_days,weekly_max_days FROM employees WHERE id=2').fetchone(),
                             ('既存の登録者',21,5))
            c.close()
