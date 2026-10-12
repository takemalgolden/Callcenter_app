"""Dependency-free regression checks for the 10-minute flexible proposal engine."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shift_planner_app'))
import flexible_optimizer as f


def person(uid,skills,start='09:20',end='18:00',rest=60):
    return {'employee_id':uid,'name':f'従業員{uid}', 'skills':skills,'start_time':start,
            'end_time':end,'break_minutes':rest}


def rules(skills, start='09:20', end='18:00',need=1):
    return [{'weekday':0,'skill':skill,'start_time':start,'end_time':end,'min_count':need}
            for skill in skills]

class FlexibleTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(f.skills_of('Consumer、MERCHANT/pp'),f.SKILLS)

    def test_time_specific_skill_change(self):
        people=[person(1,'consumer,merchant',rest=0)]
        demand=rules(['consumer'],'09:20','13:00')+rules(['merchant'],'13:00','18:00')
        out=f.build_day_plan('2026-10-12',people,demand)
        by_time={row['time']:row for row in out['staffing']}
        self.assertEqual(by_time['10:00']['assigned']['consumer'],1)
        self.assertEqual(by_time['16:00']['assigned']['merchant'],1)
        # Both queues remain enabled; no exclusive switch is required.
        self.assertTrue(any(set(seg['skills'])=={'consumer','merchant'} for seg in out['segments'] if seg['skill']!='休憩'))

    def test_multi_skill_person_not_double_counted(self):
        people=[person(1,'consumer,merchant,PP',rest=0)]
        req={'consumer':1,'merchant':1,'PP':1}
        chosen,short=f.match_slot(people,f.OPEN,req)
        self.assertEqual(len(chosen),1)
        self.assertEqual(sum(short.values()),2)

    def test_break_not_counted_and_never_outside_shift(self):
        people=[person(1,'consumer',rest=60)]
        out=f.build_day_plan('2026-10-12',people,rules(['consumer']))
        self.assertGreater(len(out['shortages']),0)
        self.assertTrue(any(s['skill']=='休憩' for s in out['segments']))
        lunch=next(x for x in out['breaks']['1'] if x['type']=='昼休憩')
        possible=f.lunch_candidates(people[0],60)
        self.assertIn(f.minute(lunch['start']),possible)
        self.assertEqual(f.minute(lunch['end'])-f.minute(lunch['start']),60)
        self.assertEqual(sum(v['minutes'] for v in out['breaks']['1'] if not v['paid']),60)

    def test_late_shift_includes_night(self):
        people=[person(1,'merchant','14:00','23:00')]
        out=f.build_day_plan('2026-10-12',people,rules(['merchant'],'18:00','23:00'))
        row=next(x for x in out['staffing'] if x['time']=='22:50')
        self.assertEqual(row['assigned']['merchant'],1)
        self.assertTrue(any('昼休憩'==b['type'] for b in out['breaks']['1']))

    def test_manual_skill_override(self):
        people=[person(1,'consumer,merchant',rest=0)]
        out=f.build_day_plan('2026-10-12',people,rules(['merchant']),
            [{'employee_id':1,'start_time':'10:00','end_time':'11:00','skill':'consumer'}])
        row=next(x for x in out['staffing'] if x['time']=='10:20')
        self.assertEqual(row['assigned']['consumer'],1)
        self.assertEqual(row['shortage']['merchant'],0)
        segment=next(seg for seg in out['segments'] if seg['start']<='10:20'<seg['end'])
        self.assertEqual(set(segment['skills']),{'consumer','merchant'})
        self.assertEqual(segment['preferred_skill'],'consumer')

    def test_different_weekday_rules(self):
        x=f.demands_for_day(rules(['consumer'],need=2),weekday=0)
        y=f.demands_for_day(rules(['consumer'],need=2),weekday=1)
        self.assertEqual(x[600]['consumer'],2)
        self.assertEqual(y[600]['consumer'],0)

    def test_gap_not_claimed_as_success(self):
        x=f.build_day_plan('2026-10-12',[],rules(['consumer']))
        self.assertFalse(x['coverage_met'])
        self.assertEqual(x['shortages'][0]['required'],1)

if __name__=='__main__':unittest.main()
