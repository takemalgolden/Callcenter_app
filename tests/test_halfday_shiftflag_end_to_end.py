"""Dependency-free integration tests: approved leave -> actual monthly draft rows.

The application is Flask based, but this harness executes the real `generate`
route body and SQLite queries without requiring Flask in the test environment.
Only HTTP decorators/flash and the unrelated coverage optimizer are stubbed.
"""
import ast
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'shift_planner_app'))
import monthly_workdays
SOURCE = ROOT / 'shift_planner_app' / 'app.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
FUNCTIONS = {
    'month_days', 'mins', '_hours_as_minutes', '_pattern_total_minutes',
    '_pattern_shift_allowed', '_pattern_hours_match', '_available_candidates',
    '_half_day_pattern_match', '_day_candidate_patterns', '_request_penalty',
    '_consecutive_days', 'generate'
}
nodes = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
        node.decorator_list = []
        nodes.append(node)
    elif isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in
        {'FULL_DAY_LEAVE_TYPES', 'HALF_DAY_LEAVE_TYPES', 'HALF_DAY_SPLIT'}
        for t in node.targets
    ):
        nodes.append(node)

# Stable fake coverage engine: 0 requirements isolates leave and shift-kind
# constraints without changing the application's real monthly selection logic.
SLOTS = list(range(9 * 60, 23 * 60, 10))
SKILLS = ['consumer', 'merchant', 'PP']
flex = types.SimpleNamespace(
    SLOTS=SLOTS,
    SKILLS=SKILLS,
    minute=lambda time: int(time.split(':')[0]) * 60 + int(time.split(':')[1]),
    clock=lambda t: f'{t // 60:02d}:{t % 60:02d}',
    skills_of=lambda skills: [x for x in (skills or 'consumer').split(',') if x in SKILLS],
    demands_for_day=lambda rows, weekday: {slot: {skill: 0 for skill in SKILLS} for slot in SLOTS},
    simultaneous_queue_coverage=lambda people, slot, rules: (
        {skill: 0 for skill in SKILLS}, {skill: 0 for skill in SKILLS}
    ),
    choose_primary_skills=lambda people, demand: {p['employee_id']: 'consumer' for p in people},
    build_day_plan=lambda day, people, rules: {'shortages': [], 'capacity_warnings': [],
                                               'issues': [], 'skill_switches': 0},
)

class Request:
    form = {'month': '2026-11'}

request = Request()
flashes = []
ctx = dict(
    calendar=calendar, date=date, datetime=datetime, timedelta=timedelta,
    Decimal=Decimal, InvalidOperation=InvalidOperation, ROUND_HALF_UP=ROUND_HALF_UP,
    json=json, sqlite3=sqlite3, flex=flex, monthly_workdays=monthly_workdays,
    request=request, session={'username':'admin','role':'admin'},
    flash=lambda msg, *args: flashes.append(str(msg)),
    url_for=lambda name, **kw: name,
    redirect=lambda dest: ('redirect', dest),
    shift_generation_readiness=lambda employee, patterns: {'reasons': [], 'compatible_employees': len(employee)},
    _refresh_day_plan=lambda conn, day, status='draft': {'shortages': [], 'skill_switches': 0},
)
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), 'exec'), ctx)

PATS = [
    ('標準・早番', '09:00', '18:00', 60, '早番'),
    ('標準・中番', '11:00', '20:00', 60, '中番'),
    ('標準・遅番', '14:00', '23:00', 60, '遅番'),
    ('半日・早番（午後休）', '09:00', '13:00', 0, '早番'),
    ('半日・早番（午前休）', '13:00', '17:00', 0, '早番'),
    ('半日・中番（午後休）', '11:00', '13:00', 0, '中番'),
    ('半日・中番（午前休）', '13:00', '20:00', 0, '中番'),
    ('半日・遅番（午前休）', '14:00', '23:00', 60, '遅番'),
]

class HalfDayRealGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite3'
        with sqlite3.connect(self.path) as c:
            c.executescript('''
                CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT,active INTEGER,
                    skills TEXT,weekly_max_days INTEGER,monthly_max_days INTEGER,
                    min_hours REAL,max_hours REAL,max_consecutive INTEGER,
                    early_ok INTEGER,mid_ok INTEGER,late_ok INTEGER);
                CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY,name TEXT,
                    start_time TEXT,end_time TEXT,break_minutes INTEGER,active INTEGER,
                    shift_type TEXT);
                CREATE TABLE requests(employee_id INTEGER,request_date TEXT,
                    request_type TEXT,status TEXT);
                CREATE TABLE assignments(employee_id INTEGER,work_date TEXT,
                    pattern_id INTEGER,skill TEXT,status TEXT);
                CREATE TABLE coverage(weekday INTEGER,start_time TEXT,end_time TEXT,
                    skill TEXT,min_count INTEGER);
                INSERT INTO employees VALUES (1,'A',1,'consumer',7,29,2,9,31,1,1,1);
            ''')
            c.executemany('''INSERT INTO shift_patterns(name,start_time,end_time,break_minutes,active,shift_type)
                             VALUES(?,?,?,?,1,?)''', PATS)
        def db():
            c=sqlite3.connect(self.path)
            c.row_factory=sqlite3.Row
            return c
        ctx['db']=db
        flashes.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def execute_generation(self):
        return ctx['generate']()

    def sql(self, sql, args=()):
        with ctx['db']() as c:
            return [dict(x) for x in c.execute(sql, args)]

    def add_approval(self, day, kind):
        with ctx['db']() as c:
            c.execute("INSERT INTO requests VALUES(?,?,?,'approved')", (1, day, kind))

    def set_flags(self, early, middle, late):
        with ctx['db']() as c:
            c.execute('UPDATE employees SET early_ok=?,mid_ok=?,late_ok=? WHERE id=1',
                      (early,middle,late))

    def assignments(self):
        return self.sql('''SELECT a.work_date,p.name,p.shift_type,p.start_time,p.end_time
            FROM assignments a JOIN shift_patterns p ON p.id=a.pattern_id
            ORDER BY a.work_date''')

    def test_all_three_allowed_approved_morning_afternoon_and_full_day(self):
        # 30 calendar days / monthly cap 29 / 1 approved full day = exactly
        # 29 possible days, so both approved half days MUST receive valid shifts.
        self.add_approval('2026-11-03','午前休')
        self.add_approval('2026-11-04','午後休')
        self.add_approval('2026-11-05','通常の休み希望')
        self.assertEqual(self.execute_generation()[0], 'redirect')
        rows=self.assignments()
        self.assertEqual(len(rows),29,flashes)
        self.assertNotIn('2026-11-05',{r['work_date'] for r in rows})
        for r in rows:
            if r['work_date']=='2026-11-03': self.assertGreaterEqual(r['start_time'],'13:00')
            if r['work_date']=='2026-11-04': self.assertLessEqual(r['end_time'],'13:00')
        self.assertIn('2026-11-03',{r['work_date'] for r in rows})
        self.assertIn('2026-11-04',{r['work_date'] for r in rows})

    def test_early_and_mid_allowed_late_disallowed(self):
        self.set_flags(1,1,0)
        self.add_approval('2026-11-03','午前休')
        self.add_approval('2026-11-04','午後休')
        self.add_approval('2026-11-05','確定休')
        self.execute_generation()
        rows=self.assignments()
        self.assertEqual(len(rows),29,flashes)
        self.assertTrue(all(r['shift_type'] in ('早番','中番') for r in rows))
        self.assertGreaterEqual(next(r for r in rows if r['work_date']=='2026-11-03')['start_time'],'13:00')
        self.assertLessEqual(next(r for r in rows if r['work_date']=='2026-11-04')['end_time'],'13:00')

    def test_late_only_never_uses_early_or_mid_even_when_half_day_requested(self):
        self.set_flags(0,0,1)
        self.add_approval('2026-11-03','午前休')
        self.add_approval('2026-11-04','午後休')
        self.execute_generation()
        rows=self.assignments()
        self.assertEqual(len(rows),29,flashes)
        self.assertTrue(all(r['shift_type']=='遅番' for r in rows))
        self.assertNotIn('2026-11-04',{r['work_date'] for r in rows})
        self.assertGreaterEqual(next(r for r in rows if r['work_date']=='2026-11-03')['start_time'],'13:00')

    def test_no_suitable_half_day_fails_without_destroying_previous_draft(self):
        self.set_flags(0,0,1)
        with ctx['db']() as c:
            c.execute('UPDATE employees SET monthly_max_days=NULL')  # 30-day target
            c.execute("INSERT INTO assignments VALUES(1,'2026-11-06',3,'consumer','draft')")
        self.add_approval('2026-11-04','午後休')
        self.execute_generation()
        self.assertEqual(len(self.assignments()),1)
        self.assertIn('勤務日数を満たすシフトを作成できませんでした', ' '.join(flashes))

    def test_shift_name_middle_keyword_matches_legacy_and_explicit_categories(self):
        check=ctx['_pattern_shift_allowed']
        person={'early_ok':0,'mid_ok':1,'late_ok':0}
        self.assertFalse(check(person,{'name':'半日・早番（午後休）'}))
        self.assertTrue(check(person,{'name':'半日・中番（午前休）'}))
        self.assertFalse(check(person,{'name':'半日・遅番（午前休）'}))
        self.assertFalse(check(person,{'name':'好きな名前','shift_type':'早番'}))
        self.assertTrue(check(person,{'name':'好きな名前','shift_type':'中番'}))
        self.assertFalse(check(person,{'name':'早番と中番兼用','shift_type':'auto'}))

class SQLitePathDisplayTests(unittest.TestCase):
    def test_startup_sqlite_display_is_shortened_without_changing_real_db(self):
        source=(ROOT/'unified.py').read_text(encoding='utf-8')
        self.assertIn("ntpath.join('シフト休憩提案アプリ統合版', 'dist', 'CallCenterUnified', 'data', 'unified.sqlite3')",source)
        self.assertIn("DB=ROOT/'data'/'unified.sqlite3'",source)
        self.assertIn('print(\'シフト・休憩は同一のSQLiteファイルを使用:\', shown_db)',source)
        self.assertNotIn("print('シフト・休憩は同一のSQLiteファイルを使用:',DB)",source)

if __name__=='__main__': unittest.main()

class HalfDayBreakPlanCompatibilityTests(unittest.TestCase):
    def test_real_break_engine_respects_halfday_working_window(self):
        import flexible_optimizer as real_engine
        for kind, begin, end in [('午前休', '13:00','20:00'),('午後休','09:20','13:00')]:
            with self.subTest(kind=kind):
                worker={'employee_id':100,'name':'半休テスト',
                        'skills':'consumer,merchant', 'start_time':begin,
                        'end_time':end,'break_minutes':0}
                plan=real_engine.build_day_plan('2026-11-10',[worker],[])
                self.assertEqual(plan['issues'],[])
                for segment in plan['segments']:
                    self.assertGreaterEqual(segment['start'],begin)
                    self.assertLessEqual(segment['end'],end)
                for rest in plan['breaks']['100']:
                    self.assertGreaterEqual(rest['start'],begin)
                    self.assertLessEqual(rest['end'],end)

class TemplateAndMigrationTests(unittest.TestCase):
    def test_pattern_ui_and_legacy_migration_present(self):
        from jinja2 import Environment
        page=(ROOT/'shift_planner_app'/'templates'/'patterns.html').read_text(encoding='utf-8')
        Environment().parse(page)
        self.assertIn('name="shift_type"',page)
        src=(ROOT/'shift_planner_app'/'app.py').read_text(encoding='utf-8')
        self.assertIn('ALTER TABLE shift_patterns ADD COLUMN shift_type',src)
        self.assertIn('shift_type=?',src)

class SQLiteUpgradeCompatibilityTests(unittest.TestCase):
    def test_existing_shift_patterns_survive_new_category_column(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('''CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,start_time TEXT NOT NULL,end_time TEXT NOT NULL,
                break_minutes INTEGER NOT NULL DEFAULT 60,active INTEGER DEFAULT 1)''')
            con.execute("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes) VALUES('半日早番','09:00','13:00',0)")
            con.execute("ALTER TABLE shift_patterns ADD COLUMN shift_type TEXT NOT NULL DEFAULT 'auto'")
            row=con.execute('SELECT name,start_time,end_time,break_minutes,shift_type FROM shift_patterns').fetchone()
            self.assertEqual(row,('半日早番','09:00','13:00',0,'auto'))
