"""Operator data scoping must be applied in SQL, not only in HTML."""
import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('operator_shift_visibility',ROOT/'shift_planner_app'/'operator_shift_visibility.py')
visibility=importlib.util.module_from_spec(spec)
spec.loader.exec_module(visibility)

class OperatorOwnShiftCalendarTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''
        CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE shift_patterns(id INTEGER PRIMARY KEY, name TEXT, start_time TEXT, end_time TEXT);
        CREATE TABLE assignments(id INTEGER PRIMARY KEY, employee_id INTEGER,work_date TEXT,
           pattern_id INTEGER,skill TEXT,status TEXT,note TEXT);
        CREATE TABLE daily_skill_plans(work_date TEXT,plan_json TEXT);
        INSERT INTO employees VALUES(11,'自分'),(22,'他の人');
        INSERT INTO shift_patterns VALUES(3,'早番','09:20','18:00');
        INSERT INTO assignments VALUES(1,11,'2026-11-01',3,'consumer','draft',''),
           (2,22,'2026-11-01',3,'PP','finalized',''),
           (3,11,'2026-11-02',3,'consumer','finalized',''),
           (4,22,'2026-11-03',3,'merchant','draft',''),
           (5,11,'2026-12-01',3,'consumer','finalized','');
        ''')
        self.conn.execute('INSERT INTO daily_skill_plans VALUES(?,?)',
                          ('2026-11-01',json.dumps({'staffing':['confidential team detail']})))
    def tearDown(self):
        self.conn.close()
    def test_operator_reads_only_their_own_draft_and_finalized(self):
        rows=visibility.calendar_assignments(self.conn,'2026-11','operator',11)
        self.assertEqual([r['id'] for r in rows],[1,3])
        self.assertEqual([r['status'] for r in rows],['draft','finalized'])
        self.assertEqual({r['name'] for r in rows},{'自分'})
    def test_another_operator_has_no_access_to_first_operators_calendar(self):
        rows=visibility.calendar_assignments(self.conn,'2026-11','operator',22)
        self.assertEqual([r['id'] for r in rows],[2,4])
        self.assertEqual({r['name'] for r in rows},{'他の人'})
    def test_operator_missing_link_never_sees_team(self):
        self.assertEqual(visibility.calendar_assignments(self.conn,'2026-11','operator',None),[])
        self.assertEqual(visibility.calendar_assignments(self.conn,'2026-11','operator',''),[])
        self.assertEqual(visibility.calendar_assignments(self.conn,'2026-11',None,None),[])
    def test_admin_can_still_see_everyone(self):
        rows=visibility.calendar_assignments(self.conn,'2026-11','admin',None)
        self.assertEqual(sorted(r['id'] for r in rows),[1,2,3,4])
    def test_month_is_scoped_for_both_roles(self):
        self.assertEqual([r['id'] for r in visibility.calendar_assignments(self.conn,'2026-12','operator',11)],[5])
    def test_operator_does_not_load_team_previews(self):
        self.assertEqual(visibility.admin_day_previews(self.conn,'2026-11','operator'),{})
        self.assertEqual(visibility.admin_day_previews(self.conn,'2026-11',None),{})
        data=visibility.admin_day_previews(self.conn,'2026-11','admin')
        self.assertIn('2026-11-01',data)
    def test_page_keeps_admin_only_day_detail_and_operator_empty_copy(self):
        app=(ROOT/'shift_planner_app'/'app.py').read_text(encoding='utf-8')
        html=(ROOT/'shift_planner_app'/'templates'/'index.html').read_text(encoding='utf-8')
        self.assertIn('operator_shift_visibility.calendar_assignments(',app)
        self.assertIn("@admin_required\ndef daily_plan_page",app)
        self.assertIn("session.get('role') == 'admin' and by_day.get(d.isoformat())",html)
        self.assertIn('この月の自分のシフトはまだ登録されていません',html)

if __name__=='__main__': unittest.main()
