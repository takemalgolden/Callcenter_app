"""Tests for the registered employee client-side keyword filter."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from jinja2 import Environment

TEMPLATE = Path(__file__).resolve().parents[1] / 'shift_planner_app' / 'templates' / 'employees.html'


class EmployeeKeywordSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding='utf-8')

    def test_jinja_template_syntax(self):
        Environment().parse(self.html)

    def test_search_inputs_and_row_data_exist(self):
        for fragment in (
            'id="employee-keyword-filter"',
            'id="employee-keyword-clear"',
            'id="employee-filter-result"',
            'id="employee-no-match"',
            'data-employee-row',
            'data-search-name="{{ e.name }}"',
            'data-search-id="{{ e.login_username or \'\' }}"',
            'data-search-skills="{{ e.skills }}"',
            'data-search-status="{{ \'有効\' if e.active else \'無効\' }}"',
        ):
            self.assertIn(fragment, self.html)

    @unittest.skipUnless(shutil.which('node'), 'Node.js unavailable')
    def test_filtering_behavior_in_javascript(self):
        scripts = re.findall(r'<script>(.*?)</script>', self.html, re.S)
        self.assertGreaterEqual(len(scripts), 2)
        js = scripts[-1]
        harness = r'''
const assert = require('node:assert/strict');
const createEventTarget = () => ({
  listeners: {},
  addEventListener(name, callback) { this.listeners[name] = callback; },
  trigger(name) { this.listeners[name](); },
});
const input = Object.assign(createEventTarget(), {value: '', focus() {this.focused = true;}});
const clear = createEventTarget();
const result = {textContent: ''};
const noMatch = {hidden: true};
const rows = [
  {dataset:{searchName:'田中 花子',searchId:'TANAKA01',searchSkills:'consumer,merchant',searchStatus:'有効'}, style:{}, hidden:false},
  {dataset:{searchName:'佐藤 太郎',searchId:'satou02',searchSkills:'PP',searchStatus:'無効'}, style:{}, hidden:false},
  {dataset:{searchName:'鈴木 次郎',searchId:'suzuki03',searchSkills:'merchant',searchStatus:'有効'}, style:{}, hidden:false},
];
global.document = {
  getElementById(id) {
    return ({'employee-keyword-filter':input,'employee-keyword-clear':clear,
      'employee-filter-result':result,'employee-no-match':noMatch})[id];
  },
  querySelectorAll(selector) {
    assert.equal(selector, '[data-employee-row]');
    return rows;
  }
};
const visible = () => rows.filter(row => !row.hidden).map(row => row.dataset.searchName);
const search = keyword => { input.value=keyword; input.trigger('input'); return visible(); };
'''
        checks = r'''
assert.equal(result.textContent, '表示 3 / 3 名');
assert.deepEqual(search('田中'), ['田中 花子']);
assert.deepEqual(search('tanaka01'), ['田中 花子']);
assert.deepEqual(search('ＴＡＮＡＫＡ０１'), ['田中 花子']);
assert.deepEqual(search('merchant'), ['田中 花子','鈴木 次郎']);
assert.deepEqual(search('merchant 田中'), ['田中 花子']);
assert.deepEqual(search('無効'), ['佐藤 太郎']);
assert.deepEqual(search('pp'), ['佐藤 太郎']);
assert.deepEqual(search('存在しない'), []);
assert.equal(noMatch.hidden, false);
assert.equal(result.textContent, '表示 0 / 3 名');
clear.trigger('click');
assert.equal(input.value, '');
assert.equal(input.focused, true);
assert.deepEqual(visible(), ['田中 花子','佐藤 太郎','鈴木 次郎']);
assert.equal(noMatch.hidden, true);
assert.equal(result.textContent, '表示 3 / 3 名');
console.log('employee keyword search behavior: OK');
'''
        result = subprocess.run(['node', '-e', harness + '\n' + js + '\n' + checks],
                                check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
