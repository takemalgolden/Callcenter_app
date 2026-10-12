"""A multi-skilled person waits on multiple queues without taking two calls."""
from pathlib import Path
import importlib.util
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'shift_planner_app'))
import flexible_optimizer as f
spec=importlib.util.spec_from_file_location('test_parallel_server',ROOT/'break_engine/server.py')
br=importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

def person(uid, skills, rest=0, start='09:20', end='13:00',primary='consumer'):
    return {'employee_id':uid, 'name':f'人{uid}', 'skills':skills,
            'primary_skill':primary, 'start_time':start,'end_time':end,'break_minutes':rest}

def requirements(skills=('consumer','merchant'), count=1):
    return [{'weekday':0,'skill':sk,'start_time':'09:20','end_time':'13:00',
             'min_count':count} for sk in skills]

class ParallelQueueTests(unittest.TestCase):
    def test_both_queues_ready_same_ten_minute_slot(self):
        out=f.build_day_plan('2026-10-12',[person(1,'consumer,merchant')],requirements())
        t=next(x for x in out['staffing'] if x['time']=='10:00')
        self.assertEqual(t['assigned']['consumer'],1)
        self.assertEqual(t['assigned']['merchant'],1)
        self.assertEqual(t['distinct_people'],1)
        self.assertEqual(t['shortage']['consumer'],0)
        self.assertEqual(t['shortage']['merchant'],0)
        self.assertEqual(t['simultaneous_deficit'],1)
        self.assertTrue(out['queue_minimums_met'])
        self.assertFalse(out['simultaneous_capacity_met'])
        self.assertFalse(out['coverage_met'])
        self.assertEqual(set(out['segments'][0]['skills']),{'consumer','merchant'})

    def test_two_distinct_people_satisfy_simultaneous_calls(self):
        out=f.build_day_plan('2026-10-12',[person(1,'consumer,merchant'),
                    person(2,'consumer',primary='consumer')],requirements())
        slot=out['staffing'][0]
        self.assertEqual(slot['assigned']['consumer'],2)
        self.assertEqual(slot['assigned']['merchant'],1)
        self.assertEqual(slot['distinct_people'],2)
        self.assertEqual(slot['simultaneous_deficit'],0)
        self.assertEqual(slot['shortage']['merchant'],0)

    def test_break_disables_all_queues_at_once(self):
        people=[person(1,'consumer,merchant',rest=60,start='09:20',end='18:00')]
        plan=f.build_day_plan('2026-10-12',people,requirements())
        break_start=plan['breaks']['1'][0]['start']
        row=next(x for x in plan['staffing'] if x['time']==break_start)
        self.assertEqual(row['assigned']['consumer'],0)
        self.assertEqual(row['assigned']['merchant'],0)
        seg=next(x for x in plan['segments'] if x['start']<=break_start<x['end'])
        self.assertEqual(seg['skill'],'休憩')
        self.assertEqual(seg['skills'],[])

    def test_preferred_manual_routing_not_exclusive(self):
        manual=[{'employee_id':1,'start_time':'10:00','end_time':'11:00','skill':'merchant'}]
        plan=f.build_day_plan('2026-10-12',[person(1,'consumer,merchant')],requirements(),manual)
        seg=next(x for x in plan['segments'] if x['start']<='10:10'<x['end'])
        self.assertEqual(seg['preferred_skill'],'merchant')
        self.assertEqual(set(seg['skills']),{'consumer','merchant'})
        row=next(x for x in plan['staffing'] if x['time']=='10:10')
        self.assertEqual(row['assigned']['consumer'],1)
        self.assertEqual(row['assigned']['merchant'],1)

    def test_break_server_schedule_has_both_queue_labels(self):
        plan=f.build_day_plan('2026-10-12',[person(1,'consumer,merchant')],requirements())
        cc_users=[{'id':1,'display_name':'テスト','shift_start':'09:20','shift_end':'13:00'}]
        result=br._break_result_from_plan('2026-10-12',cc_users,[],plan)
        entry=next(x for x in result['schedule'] if x['time']=='09:20')
        self.assertEqual(entry['skill'],'Consumer / Merchant')
        self.assertEqual(set(entry['skills']),{'consumer','merchant'})
        self.assertFalse(result['minimums_met'])
        self.assertTrue(any(x['warning_type']=='simultaneous_capacity' for x in result['shortages']))

    def test_engines_identical(self):
        self.assertEqual((ROOT/'shift_planner_app/flexible_optimizer.py').read_bytes(),
                         (ROOT/'break_engine/flexible_optimizer.py').read_bytes())

    def test_shift_daily_view_labels_readiness_not_assignment(self):
        ui=(ROOT/'shift_planner_app/templates/daily_plan.html').read_text('utf-8')
        self.assertIn('同時着信リスク', (ROOT/'shift_planner_app/templates/index.html').read_text('utf-8'))
        self.assertIn('受電可能キュー',ui)
        self.assertIn('slot.distinct_people',ui)

if __name__=='__main__':unittest.main()
