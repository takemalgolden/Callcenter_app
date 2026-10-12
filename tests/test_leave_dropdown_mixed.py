"""Regression tests for per-calendar-date selection and mixed-category submissions."""
import unittest
from test_leave_calendar_halfday import context, request, Form, PostTests, flashes


class MixedRequestTests(PostTests):
    def test_mixed_full_and_half_day_same_submission(self):
        request.form = Form(request_dates=['2026-11-03', '2026-11-04', '2026-11-05'],
                            request_types=['午前休', '午後休', '通常の休み希望'], note='混在の希望')
        self.assertEqual(context['my_requests_page']()[0], 'redirect')
        self.assertEqual([(r['request_date'], r['request_type']) for r in self.submitted()],
                         [('2026-11-03','午前休'), ('2026-11-04','午後休'),
                          ('2026-11-05','通常の休み希望')])

    def test_mixed_day_shortfall_requires_reason(self):
        all_dates = [f'2026-11-{d:02d}' for d in range(1,13)]
        # 11 full-day requests in a 30-day month -> maximum 19 workdays < 20.
        kinds = ['通常の休み希望'] * 11 + ['午前休']
        request.form = Form(request_dates=all_dates, request_types=kinds)
        self.assertEqual(context['my_requests_page']()[0], 'template')
        self.assertEqual(self.submitted(), [])
        request.form['shortfall_reason'] = '事情により'
        self.assertEqual(context['my_requests_page']()[0], 'redirect')
        self.assertEqual(len(self.submitted()), 12)
        self.assertEqual(self.submitted()[-1]['request_type'], '午前休')
        self.assertIn('【月間勤務日数不足の理由】事情により', self.submitted()[-1]['note'])

    def test_incomplete_type_list_must_not_be_accepted(self):
        request.form = Form(request_dates=['2026-11-01', '2026-11-02'],
                            request_types=['午後休'], request_type='確定休')
        self.assertEqual(context['my_requests_page']()[0], 'template')
        self.assertEqual(self.submitted(), [])
        self.assertTrue(any('区分が不足' in f[0] for f in flashes))

    def test_invalid_category_or_duplicate_rejected(self):
        request.form = Form(request_dates=['2026-11-04'], request_types=['invalid'])
        self.assertEqual(context['my_requests_page']()[0], 'template')
        self.assertEqual(self.submitted(), [])
        request.form = Form(request_dates=['2026-11-04', '2026-11-04'],
                            request_types=['午前休', '午後休'])
        self.assertEqual(context['my_requests_page']()[0], 'template')
        self.assertEqual(self.submitted(), [])

    def test_conflicting_existing_date_does_not_partially_commit(self):
        request.form = Form(request_dates=['2026-11-04'], request_types=['午前休'])
        self.assertEqual(context['my_requests_page']()[0], 'redirect')
        request.form = Form(request_dates=['2026-11-04', '2026-11-05'],
                            request_types=['午後休', '有給休暇'])
        self.assertEqual(context['my_requests_page']()[0], 'template')
        self.assertEqual(len(self.submitted()), 1)

    def test_rejected_submission_keeps_draft_date_and_kind(self):
        request.form = Form(request_dates=['2026-11-01'], request_types=['午前休'])
        self.assertEqual(context['my_requests_page']()[0], 'redirect')
        request.form = Form(request_dates=['2026-11-01', '2026-11-10'],
                            request_types=['午後休', '午前休'], note='再確認')
        res = context['my_requests_page']()
        self.assertEqual(res[0], 'template')
        self.assertEqual(res[2]['draft_selections'], {'2026-11-01': '午後休', '2026-11-10': '午前休'})
        self.assertEqual(res[2]['draft_note'], '再確認')

    def test_monthly_shortfall_mixed_category_math(self):
        fn=context['_leave_day_shortfalls']
        dates=[f'2026-11-{d:02d}' for d in range(1,13)]
        categories={day: '通常の休み希望' for day in dates[:10]}
        categories.update({day: '午前休' for day in dates[10:]})
        self.assertFalse(fn(20,[],dates,categories))
        categories[dates[10]]='午後休'
        self.assertFalse(fn(20,[],dates,categories))
        categories[dates[10]]='確定休'
        self.assertEqual(fn(20,[],dates,categories),[{'month':'2026-11','possible':19,'target':20}])

if __name__ == '__main__':
    unittest.main()
