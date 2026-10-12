"""DB-level regression tests for the administrator's monthly bulk-delete action."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / 'shift_planner_app' / 'bulk_shift_delete.py'
spec = importlib.util.spec_from_file_location('bulk_shift_delete', source)
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)

class MonthlyBulkDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = sqlite3.connect(Path(self.temp.name) / 'db.sqlite3', isolation_level='DEFERRED')
        self.con.row_factory = sqlite3.Row
        self.con.executescript('''
        CREATE TABLE assignments (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL,work_date TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE daily_skill_plans (work_date TEXT PRIMARY KEY,status TEXT,plan_json TEXT,updated_at TEXT);
        CREATE TABLE daily_skill_overrides (id INTEGER PRIMARY KEY, work_date TEXT,employee_id INTEGER,skill TEXT);
        CREATE TABLE cc_schedule_runs (id INTEGER PRIMARY KEY,schedule_date TEXT,status TEXT,result_json TEXT);
        CREATE TABLE approvals (id INTEGER PRIMARY KEY,work_month TEXT,admin_username TEXT,action TEXT,reason TEXT,created_at TEXT);
        INSERT INTO assignments VALUES(1,1,'2026-11-01','draft');
        INSERT INTO assignments VALUES(2,2,'2026-11-01','finalized');
        INSERT INTO assignments VALUES(3,3,'2026-11-03','draft');
        INSERT INTO assignments VALUES(4,4,'2026-12-01','finalized');
        INSERT INTO daily_skill_plans VALUES('2026-11-01','draft','{}','');
        INSERT INTO daily_skill_plans VALUES('2026-11-03','draft','{}','');
        INSERT INTO daily_skill_plans VALUES('2026-12-01','finalized','{}','');
        INSERT INTO daily_skill_overrides VALUES(1,'2026-11-01',1,'PP');
        INSERT INTO daily_skill_overrides VALUES(2,'2026-11-01',2,'PP');
        INSERT INTO daily_skill_overrides VALUES(3,'2026-11-03',3,'PP');
        INSERT INTO daily_skill_overrides VALUES(4,'2026-12-01',4,'PP');
        INSERT INTO cc_schedule_runs VALUES(1,'2026-11-01','confirmed','{}');
        INSERT INTO cc_schedule_runs VALUES(2,'2026-11-03','draft','{}');
        INSERT INTO cc_schedule_runs VALUES(3,'2026-12-01','confirmed','{}');
        INSERT INTO approvals VALUES(1,'2026-11','admin','警告確認','', '');
        INSERT INTO approvals VALUES(2,'2026-11','admin','最終確定','', '');
        INSERT INTO approvals VALUES(3,'2026-12','admin','警告確認','', '');
        ''')
        self.con.commit()
        self.recalculated = []

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def refresh(self, conn, work_date, status):
        self.recalculated.append((work_date, status))
        conn.execute('UPDATE daily_skill_plans SET status=? WHERE work_date=?', (status, work_date))

    def do(self, scope='draft', draft=2, finalized=1):
        result = bulk.remove_month(self.con, month='2026-11', scope=scope,
                                   admin_username='admin', expected_draft=draft,
                                   expected_finalized=finalized, refresh_day=self.refresh)
        self.con.commit()
        return result

    def test_draft_only_keeps_finalized_and_unrelated_month(self):
        result = self.do()
        self.assertEqual((result['draft'], result['finalized']), (2,0))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 2)
        self.assertEqual(self.con.execute("SELECT status FROM assignments WHERE work_date='2026-11-01'").fetchone()[0], 'finalized')
        self.assertEqual(self.recalculated, [('2026-11-01','finalized')])
        self.assertFalse(self.con.execute("SELECT 1 FROM daily_skill_plans WHERE work_date='2026-11-03'").fetchone())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM daily_skill_overrides WHERE work_date='2026-11-01'").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT status FROM cc_schedule_runs WHERE id=1").fetchone()[0], 'invalidated')
        self.assertEqual(self.con.execute("SELECT status FROM cc_schedule_runs WHERE id=3").fetchone()[0], 'confirmed')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM approvals WHERE action='警告確認' AND work_month='2026-11'").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM approvals WHERE action='最終確定' AND work_month='2026-11'").fetchone()[0],1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM approvals WHERE action='シフト一括削除'").fetchone()[0],1)

    def test_all_delete_removes_finalized_in_month_and_related_breaks(self):
        result = self.do('all')
        self.assertEqual((result['draft'],result['finalized'],result['invalidated_break_runs']), (2,1,2))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignments WHERE substr(work_date,1,7)='2026-11'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM daily_skill_plans WHERE substr(work_date,1,7)='2026-11'").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM daily_skill_overrides WHERE substr(work_date,1,7)='2026-11'").fetchone()[0],0)
        self.assertEqual(self.con.execute("SELECT status FROM cc_schedule_runs WHERE id=1").fetchone()[0],'invalidated')
        self.assertEqual(self.con.execute("SELECT status FROM cc_schedule_runs WHERE id=2").fetchone()[0],'invalidated')
        self.assertEqual(self.con.execute("SELECT status FROM cc_schedule_runs WHERE id=3").fetchone()[0],'confirmed')
        self.assertEqual(self.con.execute("SELECT status FROM assignments WHERE work_date='2026-12-01'").fetchone()[0],'finalized')
        self.assertEqual(self.recalculated,[])

    def test_stale_counts_do_not_modify_records(self):
        with self.assertRaisesRegex(bulk.BulkShiftDeleteError, '件数が変更'):
            self.do(draft=1)
        self.con.rollback()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],4)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM approvals').fetchone()[0],3)

    def test_bad_month_scope_or_no_shifts_do_not_modify_records(self):
        for month in ('2026-02-03','2026-13','2026-00','2026-1','-1','2026-11 OR 1=1'):
            with self.assertRaises(bulk.BulkShiftDeleteError):bulk.validate_month(month)
        with self.assertRaises(bulk.BulkShiftDeleteError):
            self.do(scope='confirmed_only')
        self.con.rollback()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],4)
        with self.assertRaises(bulk.BulkShiftDeleteError):
            bulk.remove_month(self.con, month='2027-01', scope='all', admin_username='admin',
                              expected_draft=0, expected_finalized=0, refresh_day=self.refresh)
        self.con.rollback()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],4)

    def test_legacy_db_without_break_runs_still_deletes(self):
        self.con.execute('DROP TABLE cc_schedule_runs')
        self.con.commit()
        result = self.do()
        self.assertEqual(result['invalidated_break_runs'],0)
        self.assertEqual(result['draft'],2)

if __name__=='__main__':unittest.main()
