"""Staggered lunchtime windows and optional lunch-first late-shift rest order."""
from pathlib import Path
import importlib.util
import tempfile
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import flexible_optimizer as flex
spec=importlib.util.spec_from_file_location('staggered_break_server',ROOT/'break_engine/server.py')
br=importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def p(uid,start,end,minutes=60,**extra):
    return dict(employee_id=uid,name=f'担当者{uid}',skills='consumer,merchant',
                start_time=start,end_time=end,break_minutes=minutes,**extra)


class StaggeredWindows(unittest.TestCase):
    def test_early_middle_late_9h_windows(self):
        for start,end,expected in [
            ('09:20','18:20',('11:30','13:00')),
            ('11:00','20:00',('13:00','15:30')),
            ('14:00','23:00',('15:00','17:30'))]:
            with self.subTest(start=start):
                starts=flex.lunch_candidates(p(1,start,end))
                self.assertEqual((flex.clock(starts[0]),flex.clock(starts[-1])),expected)
    def test_early_default_8h40_window(self):
        starts=flex.lunch_candidates(p(1,'09:20','18:00'))
        self.assertEqual((starts[0],starts[-1]),(flex.minute('11:30'),flex.minute('13:00')))
    def test_start_window_and_shift_boundary(self):
        for start,end in [('09:20','18:20'),('11:00','20:00'),('14:00','23:00'),
                          ('12:20','21:20'),('10:10','19:10'),('16:00','23:00')]:
            person=p(1,start,end)
            low,high=flex.LUNCH_WINDOWS[flex.shift_kind(person)]
            for when in flex.lunch_candidates(person):
                self.assertEqual(when%10,0)
                self.assertGreaterEqual(when,max(low,flex.minute(start)+60))
                self.assertLessEqual(when,min(high,flex.minute(end)-80))
    def test_explicit_shift_category_precedes_start_inference(self):
        person=p(1,'11:00','20:00',shift_type='早番')
        self.assertEqual(flex.shift_kind(person),'早番')
        self.assertEqual(flex.clock(flex.lunch_candidates(person)[0]),'12:00')
    def test_short_shift_without_valid_lunch_warns(self):
        person=p(1,'16:50','19:00')
        self.assertEqual(flex.lunch_candidates(person),[])
        plan=flex.build_day_plan('2026-10-12',[person],[])
        self.assertTrue(any('昼休憩' in issue for issue in plan['issues']))
    def test_auto_staggers_shifts_and_no_overlap(self):
        persons=[p(1,'09:20','18:20'),p(2,'11:00','20:00'),p(3,'14:00','23:00')]
        plan=flex.build_day_plan('2026-10-12',persons,[])
        self.assertFalse(plan['issues'],plan['issues'])
        for person in persons:
            rows=plan['breaks'][str(person['employee_id'])]
            dates={('morning' if r['type']=='前半10分休憩' else
                    'afternoon' if r['type']=='後半10分休憩' else 'lunch'):
                   flex.minute(r['start']) for r in rows}
            self.assertEqual(len(rows),3)
            self.assertTrue(flex.valid_break_sequence(person,dates))
            lunch=next(r for r in rows if r['type']=='昼休憩')
            self.assertIn(flex.minute(lunch['start']),flex.lunch_candidates(person))
            for one,two in zip(rows,rows[1:]):
                self.assertLessEqual(one['end'],two['start'])
    def test_late_lunch_first_then_two_micros_supported(self):
        late=p(1,'14:00','23:00')
        choices={'lunch':flex.minute('17:00'),'morning':flex.minute('18:20'),
                 'afternoon':flex.minute('20:10')}
        self.assertTrue(flex.valid_break_sequence(late,choices))
        self.assertFalse(flex.valid_break_sequence(p(2,'11:00','20:00'),choices))
    def test_late_auto_can_actually_choose_lunch_first(self):
        late=p(1,'14:00','23:00')
        req={slot:{s:0 for s in flex.SKILLS} for slot in flex.SLOTS}
        for slot in flex.SLOTS:
            if flex.minute('15:00') <= slot < flex.minute('17:00'):
                req[slot]['consumer']=1
        _,plans,issues=flex.schedule_breaks([late],req)
        self.assertFalse(issues,issues)
        items={x['type']:x for x in plans[1]}
        self.assertLessEqual(items['昼休憩']['end'],items['前半10分休憩']['start'])
        self.assertLessEqual(items['前半10分休憩']['end'],items['後半10分休憩']['start'])
        self.assertEqual(items['昼休憩']['start'],'17:00')
    def test_manual_choices_share_auto_windows(self):
        for start,end in [('09:20','18:20'),('11:00','20:00'),('14:00','23:00')]:
            user={'shift_start':start,'shift_end':end,'break_minutes':60}
            opt=br.manual_break_options(user)
            self.assertEqual([flex.minute(x) for x in opt['lunch']],
                             flex.lunch_candidates(p(1,start,end)))
    def test_unchanged_halfday_has_no_breaks(self):
        opt=br.manual_break_options({'shift_start':'09:20','shift_end':'13:00','break_minutes':0})
        self.assertFalse(any(opt.values()))

class ManualIntegration(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.old=br.DB_PATH
        br.DB_PATH=str(Path(self.dir.name)/'test.sqlite3')
        br.init_db()
        conn=br.db()
        conn.execute("DELETE FROM cc_users WHERE role='operator'")
        conn.execute('DELETE FROM cc_demand')
        for name,start,end in [('early','09:20','18:20'),('middle','11:00','20:00'),('late','14:00','23:00')]:
            conn.execute("INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES(?,?,'operator','x','x','[\"Consumer\"]',?,?)",(name,name,start,end))
        conn.commit();conn.close()
        c=br.db()
        self.ids={r['username']:r['id'] for r in c.execute("SELECT username,id FROM cc_users WHERE role='operator'")}
        c.close()
    def tearDown(self):
        br.DB_PATH=self.old
        self.dir.cleanup()
    def entries(self):
        return [
            dict(user_id=self.ids['early'], morning='10:30',lunch='11:50',afternoon='15:00'),
            dict(user_id=self.ids['middle'],morning='12:10',lunch='13:20',afternoon='17:10'),
            dict(user_id=self.ids['late'],  morning='18:30',lunch='17:00',afternoon='20:30'),
        ]
    def test_manual_late_lunch_first_is_accepted(self):
        result=br.create_manual_schedule('2026-10-12',1,self.entries())
        self.assertTrue(result['ok'],result.get('errors'))
        rows=result['result']['breaks'][str(self.ids['late'])]
        self.assertEqual([x['type'] for x in rows],['lunch','morning','afternoon'])
    def test_manual_early_start_prior_to_1130_rejected(self):
        data=self.entries();data[0]['lunch']='11:20'
        result=br.create_manual_schedule('2026-10-12',1,data)
        self.assertFalse(result['ok'])
    def test_manual_middle_start_after_window_rejected(self):
        data=self.entries();data[1]['lunch']='15:40'
        result=br.create_manual_schedule('2026-10-12',1,data)
        self.assertFalse(result['ok'])
    def test_manual_late_reversed_microbreak_order_rejected(self):
        data=self.entries();data[2]['morning']='21:00';data[2]['afternoon']='19:30'
        result=br.create_manual_schedule('2026-10-12',1,data)
        self.assertFalse(result['ok'])
        self.assertIn('休憩順序',result['errors'][0])
    def test_manual_lunch_overlap_rejected(self):
        data=self.entries();data[2]['morning']='17:20'
        result=br.create_manual_schedule('2026-10-12',1,data)
        self.assertFalse(result['ok'])

if __name__=='__main__':unittest.main()
