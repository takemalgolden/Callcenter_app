"""Run: python -m unittest discover -s tests -v (no Flask required for this test)."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SERVER = Path(__file__).resolve().parents[1] / 'break_engine' / 'server.py'
spec = importlib.util.spec_from_file_location('break_test_server', SERVER)
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


class DailyWorkTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.origdb = br.DB_PATH
        br.DB_PATH = str(Path(self.temp.name) / 'unified.sqlite3')
        br.init_db()
        with br.db() as c:
            c.executescript('''
              CREATE TABLE users (id INTEGER PRIMARY KEY,username TEXT,employee_id INTEGER);
              CREATE TABLE shift_patterns (id INTEGER PRIMARY KEY,start_time TEXT,end_time TEXT);
              CREATE TABLE assignments (id INTEGER PRIMARY KEY, employee_id INTEGER, work_date TEXT,
                pattern_id INTEGER,status TEXT);
              CREATE TABLE approvals (id INTEGER PRIMARY KEY, work_month TEXT,action TEXT);
            ''')
            c.execute("INSERT INTO users VALUES(2,'operator1',11)")
            c.execute("INSERT INTO shift_patterns VALUES(7,'09:20','18:00')")
            c.execute("INSERT INTO assignments VALUES(1,11,'2026-11-15',7,'finalized')")
            c.execute("INSERT INTO approvals VALUES(1,'2026-11','最終確定')")
            self.operator_id = c.execute("SELECT id FROM cc_users WHERE username='operator1'").fetchone()['id']
            self.admin_id = c.execute("SELECT id FROM cc_users WHERE username='admin'").fetchone()['id']
        self.server = ThreadingHTTPServer(('127.0.0.1',0),br.Handler)
        self.server.daemon_threads = True
        self.port=self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        br.SESSIONS['test-admin']={'user_id':self.admin_id,'expires':time.time()+500}
        br.SESSIONS['test-operator']={'user_id':self.operator_id,'expires':time.time()+500}

    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        br.SESSIONS.clear()
        br.DB_PATH = self.origdb
        self.temp.cleanup()

    def req(self, path, method='GET', payload=None, token='test-admin'):
        req=Request('http://127.0.0.1:'+str(self.port)+'/api/'+path,
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'X-Session-Token':token,'Content-Type':'application/json'},method=method)
        try:
            with urlopen(req) as r:return r.status,json.loads(r.read())
        except HTTPError as e:return e.code,json.loads(e.read())

    def ops(self,date):
        status,d=self.req('attendance?date='+date)
        self.assertEqual(status,200)
        return {u['username']:u for u in d['operators']}

    def test_confirmed_shift_automatically_shown_and_off_day(self):
        ops=self.ops('2026-11-15')
        self.assertEqual((ops['operator1']['shift_start'],ops['operator1']['shift_end']),('09:20','18:00'))
        self.assertEqual(ops['operator1']['source'],'finalized')
        self.assertEqual(ops['operator2']['source'],'off')
        self.assertFalse(ops['operator2']['present'])
        self.assertFalse(ops['operator2']['shift_start'])
        self.assertEqual(self.ops('2026-12-15')['operator1']['source'],'default')

    def test_lateness_change_day_only_and_reset(self):
        payload={'date':'2026-11-15','user_id':self.operator_id,'shift_start':'10:20',
                 'shift_end':'17:00','reason':'遅刻・早退','note':'テスト'}
        code,_=self.req('work-hours',method='POST',payload=payload)
        self.assertEqual(code,200)
        operator=self.ops('2026-11-15')['operator1']
        self.assertEqual(operator['source'],'override')
        self.assertEqual((operator['shift_start'],operator['shift_end']),('10:20','17:00'))
        self.assertEqual((operator['planned_start'],operator['planned_end']),('09:20','18:00'))
        self.assertEqual(self.ops('2026-11-16')['operator1']['source'],'off')
        with br.db() as c:
            self.assertEqual(c.execute('SELECT status FROM assignments').fetchone()[0],'finalized')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM cc_audit_log WHERE action="daily_work_hours_updated"').fetchone()[0],1)
        code,_=self.req('work-hours',method='DELETE',payload={'date':'2026-11-15','user_id':self.operator_id})
        self.assertEqual(code,200)
        self.assertEqual(self.ops('2026-11-15')['operator1']['shift_start'],'09:20')

    def test_validation_and_permission(self):
        p={'date':'2026-11-15','user_id':self.operator_id,'shift_start':'19:00','shift_end':'18:00','reason':'遅刻'}
        self.assertEqual(self.req('work-hours',method='POST',payload=p)[0],400)
        p.update(shift_start='10:03',shift_end='18:00')
        self.assertEqual(self.req('work-hours',method='POST',payload=p)[0],400)
        p['shift_start']='10:00'
        self.assertEqual(self.req('work-hours',method='POST',payload=p,token='test-operator')[0],403)
        self.assertEqual(self.req('work-hours',method='DELETE',payload={'date':'2026-11-15','user_id':self.operator_id},token='test-operator')[0],403)

    def test_generation_uses_date_specific_hours(self):
        p={'date':'2026-11-15','user_id':self.operator_id,'shift_start':'10:20',
           'shift_end':'17:00','reason':'遅刻'}
        self.assertEqual(self.req('work-hours',method='POST',payload=p)[0],200)
        with br.db() as c:
            users=br.dated_operators(c,'2026-11-15')
        active=[u for u in users if u['present'] and u['shift_start'] and u['shift_end']]
        self.assertTrue(any(u['id']==self.operator_id and u['shift_start']=='10:20' for u in active))
        self.assertFalse(any(u['username']=='operator2' for u in active))

if __name__=='__main__':unittest.main()
