import json
import unittest

from choice_lab.application import ROOT
from choice_lab.choice_model import build_observations, fit_model
from choice_lab.validation import (
    grouped_folds, cross_validate, temporal_holdout, validation_report,
    uniform_predictor, _log_loss_and_hit,
)


def fixture_tables():
    return json.loads((ROOT / 'fixtures/data.json').read_text())['tables']


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tables = fixture_tables()
        self.observations, self.items = build_observations(self.tables, 90)

    def test_folds_keep_each_customer_on_one_side(self):
        folds = grouped_folds(self.observations, k=5)
        self.assertEqual(len(folds), 5)
        for train, test in folds:
            train_customers = {o['customer'] for o in train}
            test_customers = {o['customer'] for o in test}
            self.assertFalse(train_customers & test_customers)
            self.assertEqual(len(train) + len(test), len(self.observations))

    def test_cross_validation_reports_no_customer_leakage(self):
        report = cross_validate(self.tables, 90, k=5)
        self.assertEqual(report['customer_leakage'], [])
        self.assertIn('choice_model', report['summary'])
        for name, summary in report['summary'].items():
            self.assertGreater(summary['mean_log_loss'], 0)
            self.assertTrue(0 <= summary['mean_hit_rate'] <= 1)

    def test_temporal_holdout_uses_only_past_displays(self):
        report = temporal_holdout(self.tables, 90)
        self.assertEqual(report['future_display_leakage'], [])
        self.assertGreater(report['split_day'], 0)
        self.assertTrue(all(observation['day'] <= report['split_day']
                            for observation in self.observations
                            if observation['day'] <= report['split_day']))
        self.assertIn('choice_model', report['metrics'])

    def test_validation_report_includes_guarantees_and_baselines(self):
        report = validation_report(self.tables, 90)
        self.assertTrue(report['leakage_guarantees'])
        for section in (report['cross_validation']['summary'],
                        report['temporal_holdout']['metrics']):
            self.assertEqual(set(section), {'choice_model', 'uniform', 'intercept_only'})

    def test_uniform_baseline_normalizes_over_choice_set_plus_outside(self):
        observation = self.observations[0]
        probabilities, outside = uniform_predictor(observation)
        expected = 1 / (len(observation['choices']) + 1)
        self.assertEqual(outside, expected)
        self.assertTrue(all(abs(p - expected) < 1e-12 for p in probabilities.values()))

    def test_grouped_validation_refits_without_test_customers(self):
        # 训练折里不应出现测试顾客的任何到店。
        folds = grouped_folds(self.observations, k=5)
        train, test = folds[0]
        test_customers = {o['customer'] for o in test}
        fitted = fit_model(train, self.items)
        used_visits = {o['visit'] for o in fitted['model'].observations}
        self.assertFalse(any(o['customer'] in test_customers for o in train))
        self.assertTrue(all(o['visit'] not in used_visits for o in test))

    def test_log_loss_penalizes_zero_probability_choice_safely(self):
        observation = {'choices': {'i1': 10.0}, 'choice': 'i1'}
        metrics = _log_loss_and_hit(
            [observation], lambda row: ({'i2': 1.0}, 0.0))
        self.assertGreater(metrics['log_loss'], 10)


if __name__ == '__main__':
    unittest.main()
