import copy
import json
import math
import unittest

from choice_lab.application import ROOT
from choice_lab.choice_model import (
    build_observations, fit_model, sample_parameters, support_matrix,
    new_item_prior, ChoiceDataError,
)
from choice_lab.scenarios import evaluate_scenario, validate_scenario


def fixture_tables():
    return json.loads((ROOT / 'fixtures/data.json').read_text())['tables']


class ObservationTests(unittest.TestCase):
    def test_none_choice_is_an_outside_option_not_missing(self):
        observations, _ = build_observations(fixture_tables(), 90)
        self.assertEqual(len(observations), 240)
        outside_only = [o for o in observations if o['outside_only']]
        self.assertTrue(outside_only)
        self.assertTrue(all(o['choice'] is None for o in outside_only))

    def test_unavailable_and_future_display_excluded_from_choice_set(self):
        tables = {
            'visits': [
                {'visit': 'v1', 'customer': 'c', 'store': '甲', 'day': 5, 'choice': None},
                {'visit': 'v2', 'customer': 'c', 'store': '甲', 'day': 5, 'choice': None},
            ],
            'offers': [
                {'visit': 'v1', 'item': 'i1', 'price': 10, 'available': False, 'shown_at': 5},
                {'visit': 'v2', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 6},
            ],
            'items': [{'item': 'i1', 'family': '常规', 'size': 1, 'launched_at': 1}],
        }
        observations, _ = build_observations(tables, 5)
        by_visit = {o['visit']: o for o in observations}
        self.assertEqual(by_visit['v1']['choices'], {})
        self.assertEqual(by_visit['v2']['choices'], {})  # 未来陈列不进入选择集

    def test_choice_outside_choice_set_raises(self):
        tables = {
            'visits': [{'visit': 'v1', 'customer': 'c', 'store': '甲', 'day': 5, 'choice': 'i2'}],
            'offers': [{'visit': 'v1', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 5}],
            'items': [
                {'item': 'i1', 'family': '常规', 'size': 1, 'launched_at': 1},
                {'item': 'i2', 'family': '常规', 'size': 1, 'launched_at': 1},
            ],
        }
        with self.assertRaises(ChoiceDataError):
            build_observations(tables, 5)

    def test_unlaunched_item_rejected(self):
        tables = {
            'visits': [{'visit': 'v1', 'customer': 'c', 'store': '甲', 'day': 5, 'choice': 'i1'}],
            'offers': [{'visit': 'v1', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 5}],
            'items': [{'item': 'i1', 'family': '常规', 'size': 1, 'launched_at': 8}],
        }
        with self.assertRaises(ChoiceDataError):
            build_observations(tables, 5)


class ModelFitTests(unittest.TestCase):
    def setUp(self):
        self.observations, self.items = build_observations(fixture_tables(), 90)
        self.fitted = fit_model(self.observations, self.items)
        self.model = self.fitted['model']

    def test_deterministic_and_probabilities_normalized(self):
        again = fit_model(self.observations, self.items)
        self.assertEqual(again['params'], self.fitted['params'])
        for observation in self.observations[:10]:
            probabilities, outside = self.model.probabilities(self.fitted['params'], observation)
            self.assertAlmostEqual(sum(probabilities.values()) + outside, 1.0, places=10)

    def test_analytic_score_matches_finite_difference(self):
        params = self.fitted['params']
        gradient, _, _ = self.model.score_and_hessian(params)
        epsilon = 1e-6
        for index in range(self.model.dim):
            plus = params[:]
            minus = params[:]
            plus[index] += epsilon
            minus[index] -= epsilon
            numeric = (self.model.log_likelihood(plus) - self.model.log_likelihood(minus)) / (2 * epsilon)
            self.assertAlmostEqual(gradient[index], numeric, places=4)

    def test_predicted_shares_match_observed_scale(self):
        diagnostics = self.fitted['diagnostics']
        for item in self.model.item_ids + ['none']:
            self.assertLess(abs(diagnostics['predicted_shares'][item]
                                - diagnostics['observed_shares'][item]), 0.03)

    def test_parameter_draws_cover_negative_and_positive_beta_given_weak_identification(self):
        draws = sample_parameters(self.fitted, 200)
        beta_draws = [draw[self.model.index_beta] for draw in draws]
        self.assertTrue(any(b < 0 for b in beta_draws))
        self.assertEqual(len(draws), 200)

    def test_support_matrix_flags_never_coavailable_pair(self):
        support = support_matrix(self.observations, self.items)
        self.assertEqual(support['co_available']['i1|i5'], 0)
        self.assertGreater(support['co_available']['i1|i2'], 0)
        self.assertEqual(support['available_visits']['i5'], 54)

    def test_new_item_prior_is_borrowed_and_marked_wide(self):
        prior = new_item_prior('i9', {'family': '精品', 'size': 2},
                               self.fitted, self.observations)
        self.assertTrue(prior['borrowed_from'])
        self.assertEqual(prior['prior_sd'], 1.5)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.observations, self.items = build_observations(fixture_tables(), 90)
        self.fitted = fit_model(self.observations, self.items)

    def test_delist_never_coavailable_item_routes_to_unsupported_bucket(self):
        result = evaluate_scenario(self.fitted, self.observations,
                                   {'removals': ['i5']}, draws_count=100)
        diversion = result['diversion']['i5']
        # i1-i4 从未与 i5 共同可选，必须落入无观测支撑桶。
        self.assertEqual(set(diversion['to_items_unsupported']), {'i1', 'i2', 'i3', 'i4'})
        # i6 与 i5 有 3 次共同可选，属于有支撑。
        self.assertIn('i6', diversion['to_items_supported'])
        total = (diversion['supported_total']['point']
                 + diversion['unsupported_total']['point']
                 + diversion['to_outside']['point'])
        baseline_units = result['baseline']['expected_units']['i5']['point']
        self.assertAlmostEqual(total, baseline_units, places=6)
        self.assertTrue(any(flag['candidate'] == 'i1' for flag in result['support_flags']))

    def test_delist_coavailable_item_has_no_unsupported_diversion_to_main_items(self):
        result = evaluate_scenario(self.fitted, self.observations,
                                   {'removals': ['i2']}, draws_count=100)
        diversion = result['diversion']['i2']
        self.assertEqual(set(diversion['to_items_supported']), {'i1', 'i3', 'i4', 'i6'})
        # i5 与 i2 从未共同可选，唯一的无支撑承接者。
        self.assertEqual(set(diversion['to_items_unsupported']), {'i5'})

    def test_revenue_intervals_are_ordered_and_finite(self):
        result = evaluate_scenario(self.fitted, self.observations,
                                   {'removals': ['i2'], 'price_changes': {'i1': 12}},
                                   draws_count=100)
        delta = result['revenue_delta']
        self.assertLessEqual(delta['p05'], delta['point'])
        self.assertLessEqual(delta['point'], delta['p95'])
        self.assertTrue(math.isfinite(delta['p05']))

    def test_price_cut_raises_expected_units_of_target_item(self):
        baseline = evaluate_scenario(self.fitted, self.observations, {}, draws_count=50)
        priced = evaluate_scenario(self.fitted, self.observations,
                                   {'price_changes': {'i1': 9}}, draws_count=50)
        self.assertGreater(priced['counterfactual']['expected_units']['i1']['point'],
                           baseline['counterfactual']['expected_units']['i1']['point'])

    def test_new_item_uses_prior_and_appears_with_uncertainty(self):
        result = evaluate_scenario(
            self.fitted, self.observations,
            {'additions': [{'item': 'i8', 'family': '精品', 'size': 3, 'price': 27}]},
            draws_count=100)
        self.assertIn('i8', result['counterfactual']['expected_units'])
        interval = result['counterfactual']['expected_units']['i8']
        self.assertGreater(interval['p95'], interval['p05'])
        self.assertTrue(any(flag['candidate'] == 'i8' for flag in result['support_flags']))

    def test_validation_rejects_conflicting_and_unknown_actions(self):
        with self.assertRaises(ChoiceDataError):
            validate_scenario({'removals': ['i1'], 'price_changes': {'i1': 10}}, self.items)
        with self.assertRaises(ChoiceDataError):
            validate_scenario({'removals': ['unknown']}, self.items)
        with self.assertRaises(ChoiceDataError):
            validate_scenario({'additions': [{'item': 'i8'}]}, self.items)
        with self.assertRaises(ChoiceDataError):
            validate_scenario({'price_changes': {'i1': 0}}, self.items)

    def test_combined_scenario_is_deterministic(self):
        scenario = {'removals': ['i5'],
                    'additions': [{'item': 'i8', 'family': '精品', 'size': 2, 'price': 26}],
                    'price_changes': {'i2': 15}}
        first = evaluate_scenario(self.fitted, self.observations, scenario, draws_count=100)
        second = evaluate_scenario(self.fitted, self.observations, scenario, draws_count=100)
        self.assertEqual(first['revenue_delta'], second['revenue_delta'])

    def test_fixture_not_mutated(self):
        tables = fixture_tables()
        snapshot = copy.deepcopy(tables)
        fit_model(*build_observations(tables, 90))
        self.assertEqual(snapshot, tables)


class DegenerateDataTests(unittest.TestCase):
    def _minimal_tables(self):
        return {
            'visits': [
                {'visit': 'v1', 'customer': 'a', 'store': 'S', 'day': 1, 'choice': 'i1'},
                {'visit': 'v2', 'customer': 'b', 'store': 'S', 'day': 1, 'choice': None},
                {'visit': 'v3', 'customer': 'c', 'store': 'S', 'day': 2, 'choice': None},
            ],
            'offers': [
                {'visit': 'v1', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 1},
                {'visit': 'v2', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 1},
                {'visit': 'v3', 'item': 'i1', 'price': 12, 'available': True, 'shown_at': 2},
            ],
            'items': [{'item': 'i1', 'family': 'F', 'size': 1, 'launched_at': 1}],
        }

    def test_minimal_data_fits_and_normalizes(self):
        observations, items = build_observations(self._minimal_tables(), 2)
        fitted = fit_model(observations, items)
        for observation in observations:
            probabilities, outside = fitted['model'].probabilities(fitted['params'], observation)
            self.assertAlmostEqual(sum(probabilities.values()) + outside, 1.0)

    def test_removing_only_item_routes_all_demand_outside(self):
        observations, items = build_observations(self._minimal_tables(), 2)
        fitted = fit_model(observations, items)
        result = evaluate_scenario(fitted, observations, {'removals': ['i1']}, draws_count=40)
        baseline = result['baseline']['expected_units']['i1']['point']
        self.assertAlmostEqual(result['diversion']['i1']['to_outside']['point'], baseline)
        self.assertEqual(result['counterfactual']['expected_revenue']['point'], 0.0)

    def test_all_outside_choices_still_estimate_finite_parameters(self):
        tables = {
            'visits': [{'visit': 'v1', 'customer': 'a', 'store': 'S', 'day': 1, 'choice': None}],
            'offers': [{'visit': 'v1', 'item': 'i1', 'price': 10, 'available': True, 'shown_at': 1}],
            'items': [{'item': 'i1', 'family': 'F', 'size': 1, 'launched_at': 1}],
        }
        observations, items = build_observations(tables, 1)
        fitted = fit_model(observations, items)
        self.assertTrue(math.isfinite(fitted['beta_price']))
        json.dumps(fitted['diagnostics'], allow_nan=False)

    def test_empty_scope_raises_rather_than_silent_zero(self):
        # 空范围本身返回空观测；拟合阶段必须拒绝，而不是输出看似有效的零结果。
        observations, items = build_observations(self._minimal_tables(), 0)
        self.assertEqual(observations, [])
        with self.assertRaises(ChoiceDataError):
            fit_model(observations, items)


if __name__ == '__main__':
    unittest.main()
