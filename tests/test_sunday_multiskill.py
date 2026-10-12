"""Regression tests for Sunday/Saturday weekly limits and flexible skills."""
from datetime import date, timedelta
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import monthly_workdays as mw
import flexible_optimizer as fo

def p(uid, skills, home, rest=0, start='09:20',end='18:00'):
    return dict(employee_id=uid,name=str(uid),skills=skills,primary_skill=home,
                start_time=start,end_time=end,break_minutes=rest)

def rules(skill, start, end, count=1):
    return dict(weekday=0,skill=skill,start_time=start,end_time=end,min_count=count)

class SundayWeekTests(unittest.TestCase):
    def test_sunday_begins_new_week_not_pooled_with_previous_saturday(self):
        dates=[date(2026,11,d) for d in range(1,31)]
        outside={date(2026,10,30),date(2026,10,31)}
        chosen,error=mw.select_workdays(dates,{d:(1,5) for d in dates},{date(2026,11,1)},outside,9,2,5)
        self.assertIsNone(error)
        self.assertIn(date(2026,11,1),chosen)  # day 1 is Sunday
        self.assertEqual(len(chosen),9)
        count={}
        for d in chosen|outside:
            key=d-timedelta(days=(d.weekday()+1)%7)
            count[key]=count.get(key,0)+1
        self.assertLessEqual(max(count.values()),2)

    def test_saturday_and_sunday_are_different_weeks(self):
        sat=date(2026,10,31)
        sun=date(2026,11,1)
        self.assertNotEqual(sat-timedelta(days=(sat.weekday()+1)%7),
                            sun-timedelta(days=(sun.weekday()+1)%7))

    def test_all_relevant_calendar_ui_is_sunday_first(self):
        page=(ROOT/'shift_planner_app/templates/index.html').read_text('utf-8')
        leave=(ROOT/'shift_planner_app/templates/my_requests.html').read_text('utf-8')
        admin=(ROOT/'shift_planner_app/templates/employees.html').read_text('utf-8')
        self.assertIn('["日","月","火","水","木","金","土"]',page)
        self.assertIn('(days[0].weekday() + 1) % 7',page)
        self.assertIn('const sunday=new Date',leave)
        self.assertIn('週最大勤務日数（日曜〜土曜）',admin)

class MultiSkillTests(unittest.TestCase):
    def test_multi_skill_primary_is_preference_not_restriction(self):
        people=[p(1,'consumer,merchant','consumer'),p(2,'consumer','consumer')]
        need={'consumer':1,'merchant':1,'PP':0}
        selected,shortage=fo.match_slot(people,fo.OPEN,need)
        self.assertEqual(selected[1],'merchant')
        self.assertEqual(selected[2],'consumer')
        self.assertFalse(any(shortage.values()))

    def test_stay_primary_when_all_requirements_are_met(self):
        people=[p(1,'consumer,merchant','consumer'),p(2,'consumer,merchant','merchant')]
        need={'consumer':1,'merchant':1,'PP':0}
        chosen,shortage=fo.match_slot(people,fo.OPEN,need)
        self.assertEqual(chosen,{1:'consumer',2:'merchant'})
        self.assertFalse(any(shortage.values()))

    def test_chain_multi_skill_assignments(self):
        people=[p(1,'consumer,merchant','consumer'),p(2,'merchant,PP','merchant'),
                p(3,'consumer','consumer')]
        need={'consumer':1,'merchant':1,'PP':1}
        chosen,shortage=fo.match_slot(people,fo.OPEN,need)
        self.assertEqual(chosen[3],'consumer')
        self.assertEqual(chosen[1],'merchant')
        self.assertEqual(chosen[2],'PP')
        self.assertFalse(any(shortage.values()))

    def test_larger_than_two_hours_cross_skill_coverage(self):
        people=[p(1,'consumer,merchant','consumer'),p(2,'consumer','consumer')]
        plan=fo.build_day_plan('2026-10-12',people,[rules('consumer','09:20','18:00'),
                       rules('merchant','09:20','18:00')])
        # During the registered paid microbreaks there may be a real shortage.
        # The important property is long-running multi-skill eligibility.
        self.assertGreater(plan['temporary_minutes'],120)
        self.assertEqual(plan['staffing'][0]['assigned']['consumer'],2)
        self.assertEqual(plan['staffing'][0]['assigned']['merchant'],1)
        self.assertEqual(plan['staffing'][0]['distinct_people'],2)

    def test_breaks_remove_entire_operator_from_all_skills(self):
        people=[p(1,'consumer,merchant','consumer',60),p(2,'consumer','consumer',0)]
        plan=fo.build_day_plan('2026-10-12',people,[rules('consumer','09:20','18:00'),
                                      rules('merchant','09:20','18:00')])
        lunch=plan['breaks']['1'][0]
        during=next(row for row in plan['staffing'] if row['time']==lunch['start'])
        self.assertLessEqual(sum(during['assigned'].values()),1)
        self.assertEqual(during['shortage']['merchant'],1)
        self.assertFalse(plan['coverage_met'])

    def test_manual_role_override_has_priority_and_no_double_count(self):
        people=[p(1,'consumer,merchant','consumer')]
        chosen,short=fo.match_slot(people,fo.OPEN,{'consumer':1,'merchant':1,'PP':0},
                                   forced={1:'merchant'})
        self.assertEqual(chosen[1],'merchant')
        self.assertEqual(sum(short.values()),1)

    def test_break_and_shift_engine_share_identical_planner(self):
        self.assertEqual((ROOT/'break_engine/flexible_optimizer.py').read_bytes(),
                         (ROOT/'shift_planner_app/flexible_optimizer.py').read_bytes())

if __name__=='__main__':unittest.main()
