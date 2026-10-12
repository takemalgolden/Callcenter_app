"""Regression checks for the 09:20-23:00 20-person coverage investigation."""
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import flexible_optimizer as flex

RULES=[{'weekday':6,'skill':sk,'start_time':'09:20','end_time':'23:00','min_count':1}
       for sk in flex.SKILLS]
PATTERNS=[{'id':1,'name':'早番','start_time':'09:20','end_time':'18:00','break_minutes':60},
          {'id':2,'name':'中番','start_time':'11:00','end_time':'20:00','break_minutes':60},
          {'id':3,'name':'遅番','start_time':'14:00','end_time':'23:00','break_minutes':60}]

class TwentyPersonCoverageRegression(unittest.TestCase):
    def test_lunch_can_start_after_noon(self):
        person={'employee_id':1,'name':'sample','skills':'consumer',
                'start_time':'09:20','end_time':'18:00','break_minutes':60}
        candidates=flex._break_candidates(person,'昼休憩',60)
        self.assertIn(flex.minute('12:30'),candidates)
        self.assertIn(flex.minute('13:00'),candidates)
        self.assertIn(flex.minute('12:10'),candidates)
        self.assertIn(flex.minute('11:30'),candidates)  # new early-shift priority window

    def test_balanced_shift_patterns_can_cover_all_queue_minimums(self):
        people=[]
        for skill,count in [('consumer',5),('merchant',5),('PP',4)]:
            for n in range(count):
                # Start from a mid-heavy draft, leaving late and morning margins thin.
                shift=PATTERNS[0 if n<2 else 1 if n<count-1 else 2]
                people.append(dict(employee_id=len(people)+1,name=f'{skill}{n}',
                    skills=skill,pattern_id=shift['id'],start_time=shift['start_time'],
                    end_time=shift['end_time'],break_minutes=60,finalized=False))
        day='2026-11-01'
        initial=flex.build_day_plan(day,people,RULES)
        optimized,nchanges,_=flex.rebalance_work_patterns(
            people,{p['employee_id']:PATTERNS for p in people},flex.demands_for_day(RULES,6))
        after=flex.build_day_plan(day,optimized,RULES)
        self.assertGreater(nchanges,0)
        self.assertLess(len(after['shortages']),len(initial['shortages']))
        self.assertEqual(len(after['shortages']),0,after['shortages'][:4])
        self.assertFalse(after['capacity_warnings'])

    def test_no_changes_to_finalized_or_unavailable_patterns(self):
        p=dict(employee_id=1,name='A',skills='consumer',pattern_id=2,
               start_time='11:00',end_time='20:00',break_minutes=60,finalized=True)
        draft=dict(employee_id=2,name='B',skills='PP',pattern_id=2,
               start_time='11:00',end_time='20:00',break_minutes=60,finalized=False)
        people,n,_=flex.rebalance_work_patterns([p,draft],{2:[PATTERNS[1]]},
                                                flex.demands_for_day(RULES,6))
        self.assertEqual(people[0]['pattern_id'],2)
        self.assertEqual(people[1]['pattern_id'],2)
        self.assertEqual(n,0)

if __name__=='__main__':unittest.main()
