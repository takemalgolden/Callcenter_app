"""Dependency-free regression tests for shift matching and calendar preflight.

Run: python -m unittest discover -s tests -v
"""
import ast
import unittest
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'shift_planner_app' / 'app.py'
FUNCTIONS = {
    'mins', '_hours_as_minutes', '_pattern_total_minutes', '_pattern_work_minutes',
    '_pattern_shift_allowed', '_pattern_hours_match', '_duration_label',
    'shift_generation_readiness',
}
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
tree = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name in FUNCTIONS], type_ignores=[])
context = {'Decimal': Decimal, 'InvalidOperation': InvalidOperation,
           'ROUND_HALF_UP': ROUND_HALF_UP}
exec(compile(ast.fix_missing_locations(tree), str(SOURCE), 'exec'), context)


def employee(min_hours, max_hours):
    return {'name': 'テスト従業員', 'min_hours': min_hours, 'max_hours': max_hours,
            'monthly_days': 20, 'skills': 'consumer',
            'early_ok': 1, 'mid_ok': 1, 'late_ok': 1}


def pattern(start, end, break_minutes=60):
    return {'name': '早番', 'start_time': start, 'end_time': end,
            'break_minutes': break_minutes}


class FixedHoursTests(unittest.TestCase):
    def test_equal_hours_accept_exact_eight_hour_pattern(self):
        p = pattern('09:00', '17:00')  # 8h total / 7h actual work
        e = employee(8, 8)
        self.assertTrue(context['_pattern_hours_match'](e, p))
        self.assertEqual(context['shift_generation_readiness']([e], [p])['compatible_employees'], 1)

    def test_equal_hours_does_not_ignore_actual_work_time(self):
        p = pattern('09:20', '18:00')  # 8h40 total / 7h40 actual work
        e = employee(8, 8)
        self.assertFalse(context['_pattern_hours_match'](e, p))
        preflight = context['shift_generation_readiness']([e], [p])
        self.assertIn('固定勤務時間（休憩込み・8時間00分）', preflight['employees'][0]['reasons'][0])
        self.assertIn('8時間40分', preflight['employees'][0]['reasons'][0])

    def test_break_is_included_not_subtracted(self):
        e = employee(9, 9)
        self.assertTrue(context['_pattern_hours_match'](e, pattern('09:00', '18:00', 0)))
        self.assertTrue(context['_pattern_hours_match'](e, pattern('09:00', '18:00', 60)))
        self.assertTrue(context['_pattern_hours_match'](e, pattern('09:00', '18:00', 120)))
        self.assertEqual(context['_pattern_work_minutes'](pattern('09:00', '18:00', 60)), 480)
        self.assertEqual(context['_pattern_total_minutes'](pattern('09:00', '18:00', 60)), 540)

    def test_readiness_uses_inclusive_duration(self):
        e = employee(9, 9)
        p = pattern('09:00', '18:00', 60)
        result = context['shift_generation_readiness']([e], [p])
        self.assertEqual(result['compatible_employees'], 1)
        self.assertEqual(result['employees'][0]['matching_patterns'], ['早番'])

    def test_opening_coverage_diagnostic_finds_missing_eligible_early_shift(self):
        e=employee(9,9)
        # An early shift exists, but 09:20-18:00 is only 8h40 and therefore
        # cannot be assigned to a 9-hour minimum employee.
        early=pattern('09:20','18:00')
        mid=pattern('11:00','20:00')
        late=pattern('14:00','23:00')
        result=context['shift_generation_readiness']([e],[early,mid,late])
        self.assertEqual(result['critical_candidates'][0]['counts']['consumer'],0)
        self.assertEqual(result['critical_candidates'][1]['counts']['consumer'],1)

    def test_ten_minute_precision_is_stable(self):
        p = pattern('09:20', '18:00')
        self.assertTrue(context['_pattern_hours_match'](employee(8.6666667, 8.6666667), p))

    def test_range_matching_still_works(self):
        p = pattern('09:00', '18:00')
        self.assertTrue(context['_pattern_hours_match'](employee(8.5, 9), p))
        self.assertFalse(context['_pattern_hours_match'](employee(7.5, 8.5), p))


if __name__ == '__main__':
    unittest.main()
