"""The operator's break schedule is date-scoped and correctly labelled."""
from pathlib import Path
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

server_file=Path(__file__).resolve().parents[1]/'break_engine'/'server.py'
spec=importlib.util.spec_from_file_location('operator_schedule_server',server_file)
br=importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

class OperatorScheduleVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.previous_db=br.DB_PATH
        br.DB_PATH=str(Path(self.temp.name)/'work.sqlite3')
        br.init_db()
        with br.db() as c:
            c.executescript('''
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, employee_id INTEGER, role TEXT);
                CREATE TABLE assignments(employee_id INTEGER,work_date TEXT,status TEXT);
                CREATE TABLE daily_skill_plans(work_date TEXT,status TEXT,plan_json TEXT);
            ''')
            c.execute("INSERT INTO users VALUES(1,'operator1',201,'operator')")
            c.execute("INSERT INTO users VALUES(2,'operator2',202,'operator')")
            c.execute("INSERT INTO assignments VALUES(201,'2026-11-05','finalized')")
            c.execute("INSERT INTO assignments VALUES(201,'2026-11-06','finalized')")
            c.execute("INSERT INTO assignments VALUES(202,'2026-11-05','finalized')")
            c.execute('INSERT INTO daily_skill_plans VALUES(?,?,?)',(
                '2026-11-05','finalized',json.dumps({'breaks':{
                    '201':[{'type':'前半10分休憩','start':'10:00','end':'10:10','paid':True},
                           {'type':'昼休憩60分','start':'12:00','end':'13:00','paid':False}],
                    '202':[{'type':'後半10分休憩','start':'15:10','end':'15:20'}]},
                    'segments':[{'employee_id':201,'start':'10:00','end':'10:10','skill':'休憩'},
                                {'employee_id':201,'start':'10:10','end':'10:30','skill':'consumer'},
                                {'employee_id':202,'start':'15:10','end':'15:20','skill':'休憩'}]},ensure_ascii=False)))
            c.execute('INSERT INTO daily_skill_plans VALUES(?,?,?)',(
                '2026-11-06','finalized',json.dumps({'breaks':{'201':[{'type':'昼休憩60分','start':'12:30','end':'13:30'}]},'segments':[]},ensure_ascii=False)))
            self.uid1=c.execute("SELECT id FROM cc_users WHERE username='operator1'").fetchone()[0]
            self.uid2=c.execute("SELECT id FROM cc_users WHERE username='operator2'").fetchone()[0]
            self.admin=c.execute("SELECT id FROM cc_users WHERE username='admin'").fetchone()[0]
        self.http=ThreadingHTTPServer(('127.0.0.1',0),br.Handler)
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        br.SESSIONS['user1']={'user_id':self.uid1,'expires':time.time()+300}
        br.SESSIONS['user2']={'user_id':self.uid2,'expires':time.time()+300}
        br.SESSIONS['admin_test']={'user_id':self.admin,'expires':time.time()+300}

    def tearDown(self):
        self.http.shutdown();self.http.server_close()
        br.SESSIONS.clear()
        br.DB_PATH=self.previous_db
        self.temp.cleanup()

    def request(self,date=None,token='user1'):
        q='?date='+date if date is not None else ''
        req=Request(f'http://127.0.0.1:{self.http.server_port}/api/schedule/latest{q}',
                    headers={'X-Session-Token':token})
        try:
            with urlopen(req) as res:return res.status,json.loads(res.read())
        except HTTPError as exc:return exc.code,json.loads(exc.read())

    def add_confirmed(self,day,uid,break_time='11:00'):
        with br.db() as c:
            return c.execute('INSERT INTO cc_schedule_runs(schedule_date,status,result_json) VALUES(?,?,?)',(
                day,'confirmed',json.dumps({'breaks':{str(uid):[{'type':'morning','start':break_time,'end':'11:10'}]},
                   'schedule':[{'operator_id':uid,'time':break_time,'on_break':True,'skill':'待機'},
                               {'operator_id':self.uid2,'time':break_time,'on_break':False,'skill':'PP'}]}))).lastrowid

    def test_finalized_shift_shows_provisional_breaks(self):
        status,data=self.request('2026-11-05')
        self.assertEqual(status,200)
        run=data['run']
        self.assertEqual(run['status'],'provisional')
        self.assertEqual([b['start'] for b in run['result']['breaks'][str(self.uid1)]],['10:00','12:00'])
        self.assertEqual(run['result']['schedule'][0]['time'],'10:00')
        self.assertEqual(len(run['result']['schedule']),3)
        self.assertTrue(all(x['operator_id']==self.uid1 for x in run['result']['schedule']))

    def test_after_break_confirmation_show_confirmed_for_same_day(self):
        self.add_confirmed('2026-11-05',self.uid1)
        status,data=self.request('2026-11-05')
        self.assertEqual(status,200)
        self.assertEqual(data['run']['status'],'confirmed')
        self.assertEqual(data['run']['result']['breaks'][str(self.uid1)][0]['start'],'11:00')
        self.assertEqual(len(data['run']['result']['schedule']),1)

    def test_other_date_global_latest_cannot_hide_selected_day(self):
        self.add_confirmed('2026-11-05',self.uid1)
        self.add_confirmed('2026-11-06',self.uid1,break_time='14:00')
        self.assertEqual(self.request('2026-11-05')[1]['run']['date'],'2026-11-05')
        self.assertEqual(self.request('2026-11-06')[1]['run']['result']['breaks'][str(self.uid1)][0]['start'],'14:00')

    def test_operator_cannot_see_another_operators_breaks(self):
        status,data=self.request('2026-11-05','user2')
        self.assertEqual(status,200)
        b=data['run']['result']['breaks']
        self.assertEqual(set(b),{str(self.uid2)})
        self.assertEqual(b[str(self.uid2)][0]['start'],'15:10')
        self.assertTrue(all(x['operator_id']==self.uid2 for x in data['run']['result']['schedule']))
        self.add_confirmed('2026-11-05',self.uid1)
        self.assertEqual(set(self.request('2026-11-05','user2')[1]['run']['result']['breaks']),{str(self.uid2)})

    def test_date_not_finalized_has_no_provisional_plan(self):
        self.assertIsNone(self.request('2026-11-07')[1]['run'])
        self.assertEqual(self.request('invalid')[0],400)

    def test_finalized_shift_with_missing_preview_indicates_pending(self):
        with br.db() as c:
            c.execute("INSERT INTO assignments VALUES(201,'2026-11-08','finalized')")
        data=self.request('2026-11-08')[1]
        self.assertEqual(data['run']['status'],'provisional')
        self.assertIn('まだありません',data['run']['result']['note'])

if __name__=='__main__':unittest.main()
