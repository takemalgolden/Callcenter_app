"""The operator break timetable hides paid status while preserving all schedule data."""
from pathlib import Path
import unittest

PAGE = Path(__file__).resolve().parents[1] / 'break_engine' / 'static' / 'index.html'

class OperatorBreakLabelTests(unittest.TestCase):
    def test_operator_breaks_show_type_and_time_without_paid_label(self):
        page = PAGE.read_text(encoding='utf-8')
        line = next(x for x in page.splitlines() if 'const myBreaks=' in x)
        self.assertIn('esc(displayBreakLabel(x,ownBreaks))', line)
        self.assertIn('10分休憩①', page)
        self.assertIn('10分休憩②', page)
        self.assertIn('esc(x.start)', line)
        self.assertIn('esc(x.end)', line)
        self.assertNotIn('x.paid', line)
        self.assertNotIn('有給', line)

    def test_other_views_and_break_logic_untouched(self):
        page = PAGE.read_text(encoding='utf-8')
        self.assertIn('function renderResult(r,id)', page)
        self.assertIn('function confirmSchedule(id)', page)
        self.assertIn('function loadMySchedule()', page)

if __name__ == '__main__':
    unittest.main()
