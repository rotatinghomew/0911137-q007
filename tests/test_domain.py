import copy
import json
import unittest

from choice_lab.application import ROOT
from choice_lab.domain import run


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'fixtures/data.json').read_text())
        self.data = {key: [] for key in self.fixture['tables']}

    def test_fixture_is_deterministic_and_not_mutated(self):
        original = copy.deepcopy(self.fixture['tables'])
        options = {'cutoff': self.fixture['meta']['default_cutoff'], 'group': ''}
        first = run(self.fixture['tables'], options)
        second = run(self.fixture['tables'], options)
        self.assertEqual(first, second)
        self.assertEqual(original, self.fixture['tables'])
        self.assertTrue(first['rows'])
        json.dumps(first, allow_nan=False)

    def sample(self):
        self.data['visits'] = [dict(visit='v1', customer='c', store='甲', day=1, choice=None), dict(visit='v2', customer='c', store='甲', day=2, choice='i')]
        self.data['offers'] = [dict(visit='v1', item='i', price=15, available=True, shown_at=1), dict(visit='v2', item='i', price=20, available=True, shown_at=2)]

    def test_nonpurchase_keeps_an_opportunity(self):
        self.sample()
        result = run(self.data, {'cutoff': 2, 'group': ''})
        self.assertEqual(result['summary']['到店次数'], 2)
        self.assertEqual(result['summary']['未购买'], 1)
        self.assertEqual(result['summary']['已购金额'], 20)

    def test_unavailable_choice_is_not_a_valid_purchase(self):
        self.sample()
        self.data['offers'][1]['available'] = False
        result = run(self.data, {'cutoff': 2, 'group': ''})
        self.assertEqual(result['summary']['记录矛盾'], 1)
        self.assertEqual(result['summary']['已购金额'], 0)

    def test_future_display_is_not_a_choice_set(self):
        self.sample()
        self.data['offers'][1]['shown_at'] = 3
        row = run(self.data, {'cutoff': 2, 'group': '甲'})['rows'][1]
        self.assertEqual(row['available'], [])
        self.assertFalse(row['valid'])
