import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from choice_lab.application import Application, ROOT, server


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.application = Application(ROOT / 'fixtures/data.json',
                                       Path(self.directory.name) / 'state.sqlite3', ROOT / 'web')
        self.httpd = server(self.application)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'

    def tearDown(self):
        self.httpd.shutdown()
        self.worker.join()
        self.httpd.server_close()
        self.directory.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as response:
            return json.load(response)

    def post(self, path, payload):
        request = Request(self.base + path, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request) as response:
            return response.status, json.load(response)

    def test_model_endpoint_reports_support_and_diagnostics(self):
        model = self.get('/api/model')['model']
        self.assertEqual(model['support']['co_available']['i1|i5'], 0)
        self.assertIn('note_price', model['diagnostics'])
        self.assertIn('beta_price_se', model)

    def test_validation_endpoint_has_no_leakage(self):
        validation = self.get('/api/validation')['validation']
        self.assertEqual(validation['cross_validation']['customer_leakage'], [])
        self.assertEqual(validation['temporal_holdout']['future_display_leakage'], [])

    def test_scenario_endpoint_three_way_diversion(self):
        _, payload = self.post('/api/scenario', {
            'options': {'cutoff': 90},
            'scenario': {'removals': ['i5']},
        })
        diversion = payload['result']['diversion']['i5']
        self.assertEqual(set(diversion['to_items_unsupported']), {'i1', 'i2', 'i3', 'i4'})
        self.assertIn('i6', diversion['to_items_supported'])
        self.assertTrue(payload['result']['revenue_delta']['p95']
                        >= payload['result']['revenue_delta']['point'])

    def test_scenario_endpoint_accepts_combined_actions(self):
        status, payload = self.post('/api/scenario', {
            'scenario': {
                'removals': ['i2'],
                'price_changes': {'i1': 12},
                'additions': [{'item': 'i8', 'family': '精品', 'size': 2, 'price': 26}],
            },
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload['scenario_request']['removals'], ['i2'])
        self.assertIn('i8', payload['result']['counterfactual']['expected_units'])

    def test_scenario_rejects_invalid_actions(self):
        for bad in [
            {'scenario': {'removals': ['missing']}},
            {'scenario': {'price_changes': {'i1': -1}}},
            {'scenario': {'additions': [{'item': 'x'}]}},
        ]:
            with self.assertRaises(HTTPError) as caught:
                self.post('/api/scenario', bad)
            self.assertEqual(caught.exception.code, 400)

    def test_scope_validation_still_applies_to_model_routes(self):
        for path in ['/api/model?cutoff=1.5', '/api/validation?group=missing']:
            with self.assertRaises(HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 400)

    def test_legacy_view_and_assets_remain_available(self):
        view = self.get('/api/view')
        self.assertIn('summary', view['result'])
        with urlopen(self.base + '/app.js') as response:
            self.assertIn('api/scenario', response.read().decode())


if __name__ == '__main__':
    unittest.main()
