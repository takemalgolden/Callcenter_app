"""Dependency-free tests: leave selection, monthly days, partial-shift eligibility and POST route."""
import ast
import calendar
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "shift_planner_app"))
import monthly_workdays

SOURCE = Path(__file__).resolve().parents[1] / 'shift_planner_app' / 'app.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
NEEDED = {'month_days', 'mins', '_hours_as_minutes', '_pattern_total_minutes', '_pattern_shift_allowed',
          '_pattern_hours_match', '_available_candidates', '_half_day_pattern_match',
          '_day_candidate_patterns', '_request_penalty', '_parse_selected_leave_dates',
          '_leave_day_shortfalls', 'my_requests_page'}
nodes = []
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name in NEEDED:
        n.decorator_list = []
        nodes.append(n)
    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
        {'FULL_DAY_LEAVE_TYPES','HALF_DAY_LEAVE_TYPES','OPERATOR_REQUEST_TYPES','HALF_DAY_SPLIT'} for t in n.targets):
        nodes.append(n)

class Form(dict):
    def getlist(self, name):
        return self.get(name, [])

class Request:
    method = 'GET'
    form = Form()

flashes = []
request = Request()
session = {'role': 'operator', 'employee_id': 1, 'username': 'worker'}
context = {'calendar': calendar, 'date': date, 'Decimal': Decimal,
           'InvalidOperation':InvalidOperation, 'ROUND_HALF_UP':ROUND_HALF_UP,
           'request':request, 'session':session,'sqlite3':sqlite3,'timedelta':timedelta,'monthly_workdays':monthly_workdays,
           'flash':lambda *args:flashes.append(args),
           'url_for':lambda x:x,'redirect':lambda target:('redirect',target),
           'render_template':lambda *args,**kwargs:('template',args,kwargs)}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SOURCE),'exec'),context)

E = {'id':1,'monthly_days':20,'name':'A','skills':'consumer',
     'min_hours':9,'max_hours':9,'early_ok':1,'mid_ok':1,'late_ok':1}
P = [dict(id=1,name='早番',start_time='09:00',end_time='18:00',break_minutes=60),
     dict(id=2,name='半日午前',start_time='09:20',end_time='13:00',break_minutes=0),
     dict(id=3,name='半日午後',start_time='14:00',end_time='18:00',break_minutes=0)]

class LeaveTests(unittest.TestCase):
    def test_multiple_dates_and_strict_validation(self):
        fn=context['_parse_selected_leave_dates']
        self.assertEqual(fn(['2026-11-23','2026-11-04']),['2026-11-04','2026-11-23'])
        for arr in ([],['2026-02-30'],['2026-11-03','2026-11-03'],['2026-11-03',''],['2026-11-03']*94):
            with self.assertRaises(ValueError):fn(arr)

    def test_reason_trigger_for_less_than_monthly_days(self):
        fn=context['_leave_day_shortfalls']
        dates=[f'2026-11-{d:02d}' for d in range(1,12)] # 30-11=19 < 20
        self.assertEqual(fn(20,[],dates,'通常の休み希望'), [{'month':'2026-11','possible':19,'target':20}])
        self.assertFalse(fn(20,[],dates,'午前休'))
        self.assertFalse(fn(20,[],dates[:10],'通常の休み希望'))
        existing=[dict(request_date='2026-11-01',request_type='通常の休み希望',status='pending')]
        self.assertTrue(fn(20,existing,dates[1:],'確定休'))
        existing[0]['status']='rejected'
        self.assertFalse(fn(20,existing,dates[1:],'確定休'))

    def test_two_months_independent(self):
        fn=context['_leave_day_shortfalls']
        dates=[f'2026-11-{d:02d}' for d in range(1,12)]+['2026-12-01']
        self.assertEqual(len(fn(20,[],dates,'確定休')),1)

    def test_approved_half_day_relaxes_minimum_only(self):
        fn=context['_day_candidate_patterns']
        full=fn(E,P,[],'2026-11-02')
        self.assertEqual([x['id'] for x in full],[1])
        morning=fn(E,P,[dict(employee_id=1,request_date='2026-11-02',request_type='午前休',status='approved')],'2026-11-02')
        afternoon=fn(E,P,[dict(employee_id=1,request_date='2026-11-02',request_type='午後休',status='approved')],'2026-11-02')
        self.assertEqual([x['id'] for x in morning],[3])
        self.assertEqual([x['id'] for x in afternoon],[2])
        both=fn(E,P,[dict(employee_id=1,request_date='2026-11-02',request_type=kind,status='approved') for kind in ('午前休','午後休')],'2026-11-02')
        self.assertFalse(both)

    def test_rejected_half_day_ignored(self):
        fn=context['_day_candidate_patterns']
        data=[dict(employee_id=1,request_date='2026-11-02',request_type='午前休',status='rejected')]
        self.assertEqual([x['id'] for x in fn(E,P,data,'2026-11-02')],[1])

    def test_full_day_approved_blocks_pending_does_not(self):
        fn=context['_request_penalty']
        req=[dict(employee_id=1,request_date='2026-11-02',request_type='確定休',status='approved')]
        self.assertIsNone(fn(1,'2026-11-02',req))
        req[0]['status']='rejected'
        self.assertEqual(fn(1,'2026-11-02',req),0)

class PostTests(unittest.TestCase):
    def setUp(self):
        flashes.clear()
        self.tmp=tempfile.TemporaryDirectory()
        self.conn=sqlite3.connect(Path(self.tmp.name)/'app.sqlite3')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''CREATE TABLE employees(id INTEGER PRIMARY KEY,name TEXT,monthly_days INTEGER,weekly_max_days INTEGER DEFAULT 5,monthly_max_days INTEGER,active INTEGER);
        CREATE TABLE requests(id INTEGER PRIMARY KEY,employee_id INTEGER,request_date TEXT,request_type TEXT,status TEXT,note TEXT);
        INSERT INTO employees(id,name,monthly_days,active) VALUES(1,'Operator A',20,1);''')
        self.conn.commit()
        self.conn.close()
        def db():
            c=sqlite3.connect(Path(self.tmp.name)/'app.sqlite3'); c.row_factory=sqlite3.Row;return c
        context['db']=db
        request.method='POST'
        request.form=Form()

    def tearDown(self):
        request.method='GET'
        self.tmp.cleanup()

    def submitted(self):
        with context['db']() as db:
            return [dict(r) for r in db.execute('SELECT * FROM requests ORDER BY request_date')]

    def test_bulk_submission_atomic_and_duplicates(self):
        request.form=Form(request_dates=['2026-11-02','2026-11-08'],request_type='午前休',note='通院')
        self.assertEqual(context['my_requests_page']()[0],'redirect')
        self.assertEqual(len(self.submitted()),2)
        self.assertEqual(self.submitted()[0]['request_type'],'午前休')
        request.form=Form(request_dates=['2026-11-02','2026-11-09'],request_type='午後休',note='')
        self.assertEqual(context['my_requests_page']()[0],'template')
        self.assertEqual(len(self.submitted()),2)
        self.assertTrue(any('既に申請' in args[0] for args in flashes))

    def test_missing_reason_rejected_and_reason_saved(self):
        eleven=[f'2026-11-{i:02d}' for i in range(1,12)]
        request.form=Form(request_dates=eleven,request_type='通常の休み希望',note='補足')
        self.assertEqual(context['my_requests_page']()[0],'template')
        self.assertFalse(self.submitted())
        request.form['shortfall_reason']='家庭の事情により'
        self.assertEqual(context['my_requests_page']()[0],'redirect')
        self.assertEqual(len(self.submitted()),11)
        self.assertIn('【月間勤務日数不足の理由】家庭の事情により',self.submitted()[0]['note'])

    def test_half_days_do_not_require_reason(self):
        request.form=Form(request_dates=[f'2026-11-{i:02d}' for i in range(1,20)],request_type='午後休')
        self.assertEqual(context['my_requests_page']()[0],'redirect')
        self.assertEqual(len(self.submitted()),19)

if __name__=='__main__':unittest.main()
