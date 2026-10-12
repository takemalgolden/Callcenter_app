"""Primary per day, exceptional assistance, persistent base skill, break sync."""
from pathlib import Path
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import flexible_optimizer as flex
try:
    import app as shift
except ModuleNotFoundError as exc:
    if exc.name != 'flask':raise
    shift=None
spec=importlib.util.spec_from_file_location('primary_break_server',ROOT/'break_engine'/'server.py')
br=importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

def person(uid,skills,home,begin='09:20',end='18:00',rest=0):
    return dict(employee_id=uid,name=f'担当者{uid}',skills=skills,primary_skill=home,
        start_time=begin,end_time=end,break_minutes=rest)

def rule(skill,start='09:20',end='18:00',n=1):
    return dict(weekday=0,skill=skill,start_time=start,end_time=end,min_count=n)

class PrimaryRuleTests(unittest.TestCase):
    def test_primary_remains_fixed_when_staffing_meets_requirements(self):
        people=[person(1,'consumer,merchant','consumer'),person(2,'merchant,consumer','merchant')]
        plan=flex.build_day_plan('2026-10-12',people,[rule('consumer'),rule('merchant')])
        self.assertEqual(plan['primary_skills'],{'1':'consumer','2':'merchant'})
        self.assertEqual(plan['skill_switches'],0)
        self.assertFalse(any(seg['temporary'] for seg in plan['segments']))

    def test_multi_skill_can_cover_full_shortage_without_120_minute_cap(self):
        # No break: merchant needs temporary staffing, but one operator must
        # still cover consumer. The extra consumer provides a surplus donor.
        people=[person(1,'consumer','consumer'),
                person(2,'consumer,merchant','consumer'),
                person(3,'merchant','merchant','15:00','18:00')]
        plan=flex.build_day_plan('2026-10-12',people,[rule('consumer'),rule('merchant')])
        temporary=[seg for seg in plan['segments'] if seg['temporary']]
        self.assertTrue(temporary)
        # No artificial 60-minute or 120-minute cap: the eligible person may
        # keep covering Merchant as long as there is a shortage and Consumer
        # keeps its required minimum.
        total=sum(flex.minute(seg['end'])-flex.minute(seg['start'])
                  for seg in temporary if seg['employee_id']==2)
        self.assertGreater(total,120)
        self.assertEqual(plan['primary_skills']['2'],'consumer')
        # During the eligible person's scheduled breaks, true gaps remain visible.
        # Skill-ready counts overlap, but the number of distinct people does not.
        self.assertTrue(all(row['distinct_people'] <= 3 for row in plan['staffing']))
        self.assertGreater(plan['staffing'][0]['assigned']['consumer'] + plan['staffing'][0]['assigned']['merchant'],
                           plan['staffing'][0]['distinct_people'])

    def test_manual_override_preserved_even_longer_than_auto_limit(self):
        people=[person(1,'consumer,merchant','consumer')]
        plan=flex.build_day_plan('2026-10-12',people,[rule('merchant')],[dict(employee_id=1,skill='merchant',start_time='09:20',end_time='13:00')])
        # A paid 10-minute break may interrupt the explicitly assigned window.
        at_11=next(x for x in plan['staffing'] if x['time']=='11:50')
        self.assertEqual(at_11['assigned']['merchant'],1)
        self.assertEqual(plan['primary_skills']['1'],'consumer')

    def test_breaks_remove_from_primary_coverage(self):
        people=[person(1,'consumer','consumer',rest=60)]
        plan=flex.build_day_plan('2026-10-12',people,[rule('consumer')])
        self.assertTrue(plan['shortages'])
        self.assertTrue(plan['breaks']['1'])
        self.assertTrue(any(seg['skill']=='休憩' for seg in plan['segments']))
        self.assertEqual(plan['primary_skills']['1'],'consumer')

@unittest.skipIf(shift is None, "Flask not installed in this inspection environment")
class IntegrationPrimaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_shift=shift.DB_PATH;self.old_br=br.DB_PATH
        shift.DB_PATH=str(Path(self.tmp.name)/'data.sqlite3')
        br.DB_PATH=shift.DB_PATH
        shift.app.config.update(TESTING=True,SECRET_KEY='test-secret')
        shift.init_db()
        br.init_db()
        self.client=shift.app.test_client()
        with br.db() as conn:
            conn.execute("INSERT INTO employees(id,name,skills,monthly_days,min_hours,max_hours,active) VALUES (101,'複合スキル','consumer,merchant',20,0,9,1)")
            conn.execute("INSERT INTO users(username,password_hash,role,employee_id) VALUES('flex','unused','operator',101)")
            conn.execute("INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES('flex','複合スキル','operator','0','0','[\"Consumer\",\"Merchant\"]','09:20','18:00')")
            conn.execute("INSERT INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(101,'2026-10-12',1,'merchant','finalized')")
            conn.execute("INSERT INTO daily_skill_overrides(work_date,employee_id,start_time,end_time,skill) VALUES('2026-10-12',101,'15:00','15:30','consumer')")
            conn.execute("INSERT INTO cc_demand(skill,start_time,end_time,min_staff) VALUES('Merchant','09:20','18:00',1)")
            self.cc_id=conn.execute("SELECT id FROM cc_users WHERE username='flex'").fetchone()[0]
            self.admin_id=conn.execute("SELECT id FROM cc_users WHERE username='admin'").fetchone()[0]

    def tearDown(self):
        shift.DB_PATH=self.old_shift;br.DB_PATH=self.old_br;self.tmp.cleanup()

    def test_finalized_primary_visible_in_break(self):
        with br.db() as c:
            rows=br.dated_operators(c,'2026-10-12')
        u=next(x for x in rows if x['username']=='flex')
        self.assertEqual(u['primary_skill'],'merchant')

    def test_break_regeneration_keeps_primary_and_manual_override(self):
        with br.db() as c:
            rows=br.dated_operators(c,'2026-10-12')
        self.assertEqual(len(rows),7)
        # A finalized month hides all operators without a finalized assignment.
        with br.db() as c:
            c.execute("INSERT INTO approvals(work_month,admin_username,action,reason,created_at) VALUES('2026-10','admin','最終確定','','2026-10-12')")
        result=br.generate_schedule('2026-10-12',self.admin_id)
        self.assertTrue(result['ok'])
        schedule=result['result']['schedule']
        self.assertEqual(result['result']['primary_skills'][str(self.cc_id)],'merchant')
        override=next(x for x in schedule if x['operator_id']==self.cc_id and x['time']=='15:10')
        self.assertEqual(override['skill'],'Consumer')
        normal=next(x for x in schedule if x['operator_id']==self.cc_id and x['time']=='16:10')
        self.assertEqual(normal['skill'],'Merchant')
        with br.db() as c:
            row=c.execute("SELECT skill FROM assignments WHERE employee_id=101").fetchone()
            self.assertEqual(row['skill'],'merchant')

    def test_daily_primary_edit_endpoint(self):
        with self.client.session_transaction() as ses:
            ses['username']='admin';ses['role']='admin'
        response=self.client.post('/daily-plan/2026-10-12/primary',data={'employee_id':'101','skill':'consumer'})
        self.assertEqual(response.status_code,302)
        with shift.db() as c:
            self.assertEqual(c.execute('SELECT skill FROM assignments WHERE employee_id=101').fetchone()[0],'consumer')
            plan=json.loads(c.execute('SELECT plan_json FROM daily_skill_plans WHERE work_date=?',('2026-10-12',)).fetchone()[0])
            self.assertEqual(plan['primary_skills']['101'],'consumer')

if __name__=='__main__':unittest.main()

class StandaloneBreakPrimaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original=br.DB_PATH
        br.DB_PATH=str(Path(self.tmp.name)/'break.sqlite3')
        br.init_db()
        with br.db() as c:
            c.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,employee_id INTEGER);
                CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY,start_time TEXT,end_time TEXT);
                CREATE TABLE assignments(id INTEGER PRIMARY KEY,employee_id INTEGER,work_date TEXT,
                    pattern_id INTEGER,skill TEXT,status TEXT);
                CREATE TABLE approvals(id INTEGER PRIMARY KEY,work_month TEXT,action TEXT);
                CREATE TABLE daily_skill_overrides(id INTEGER PRIMARY KEY,work_date TEXT,
                    employee_id INTEGER,start_time TEXT,end_time TEXT,skill TEXT);''')
            c.execute("INSERT INTO users VALUES(1,'operator1',99)")
            c.execute("INSERT INTO shift_patterns VALUES(1,'09:20','18:00')")
            c.execute("INSERT INTO assignments VALUES(1,99,'2026-10-12',1,'merchant','finalized')")
            c.execute("INSERT INTO approvals VALUES(1,'2026-10','最終確定')")
            c.execute("INSERT INTO daily_skill_overrides VALUES(1,'2026-10-12',99,'15:00','15:30','consumer')")
            c.execute("DELETE FROM cc_demand")
            c.execute("INSERT INTO cc_demand(skill,start_time,end_time,min_staff) VALUES('Merchant','09:20','18:00',1)")
            self.uid=c.execute("SELECT id FROM cc_users WHERE username='operator1'").fetchone()[0]
            self.admin=c.execute("SELECT id FROM cc_users WHERE username='admin'").fetchone()[0]
    def tearDown(self):
        br.DB_PATH=self.original;self.tmp.cleanup()
    def test_finalized_skill_and_override_survive_break_regeneration(self):
        with br.db() as c:
            op=next(u for u in br.dated_operators(c,'2026-10-12') if u['id']==self.uid)
            self.assertEqual(op['primary_skill'],'merchant')
        result=br.generate_schedule('2026-10-12',self.admin)
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['primary_skills'][str(self.uid)],'merchant')
        schedule=result['result']['schedule']
        ten=next(row for row in schedule if row['operator_id']==self.uid and row['time']=='15:10')
        self.assertEqual(ten['skill'],'Consumer / Merchant / PP')
        self.assertEqual(ten['preferred_skill'],'consumer')
        # Lunch and paid breaks may move after the new lunch-window update.
        # Verify the preferred skill after the override at a working slot.
        late=next(row for row in schedule if row['operator_id']==self.uid and
                  row['time']>'15:30' and row['skill']!='休憩' and row['skill']!='待機')
        self.assertEqual(late['skill'],'Consumer / Merchant / PP')
        self.assertEqual(late['preferred_skill'],'merchant')
        with br.db() as c:
            self.assertEqual(c.execute('SELECT skill FROM assignments').fetchone()[0],'merchant')
