import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from choice_lab.application import Application, ROOT, server


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.application = Application(ROOT / 'fixtures/data.json', Path(self.directory.name) / 'state.sqlite3', ROOT / 'web')
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

    def test_catalog_and_snapshot_roundtrip(self):
        catalog = self.get('/api/catalog')
        view = self.get('/api/view')
        self.assertEqual(catalog['digest'], view['digest'])
        self.assertTrue(view['result']['rows'])
        payload = {'title': '业务核对', 'options': view['options']}
        request = Request(self.base + '/api/analyses', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            saved = json.load(response)
        restored = self.get('/api/analyses/' + saved['id'])
        self.assertEqual(restored['result'], view['result'])
        self.assertEqual(restored['options'], view['options'])
        self.assertEqual(len(self.get('/api/analyses')), 1)

    def test_real_assets(self):
        for path, marker in [('/', '<html lang="zh-CN">'), ('/app.js', 'fetch('), ('/style.css', ':root')]:
            with urlopen(self.base + path) as response:
                self.assertIn(marker, response.read().decode())

    def test_bad_filter_and_unknown_resource(self):
        for path, code in [('/api/view?cutoff=1.5', 400), ('/api/view?extra=1', 400), ('/api/view?group=missing', 400), ('/api/analyses/missing', 404), ('/api/source?table=missing', 404)]:
            with self.assertRaises(HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, code)

    def test_source_is_same_generation(self):
        catalog = self.get('/api/catalog')
        for table, count in catalog['tables'].items():
            source = self.get('/api/source?table=' + table)
            self.assertEqual(source['digest'], catalog['digest'])
            self.assertEqual(len(source['rows']), count)

    def test_database_persists_and_saved_result_is_frozen(self):
        value = self.application.analyze({})
        saved = self.application.store.save('保留版本', value['digest'], value['options'], value['result'])
        reloaded = Application(self.application.dataset, self.application.store.path, ROOT / 'web')
        changed = dict(value['result'])
        changed['notes'] = ['后续操作']
        reloaded.store.save('另一次分析', value['digest'], value['options'], changed)
        self.assertEqual(reloaded.store.get(saved['id'])['result'], value['result'])
        self.assertEqual(len(reloaded.store.list()), 2)

    def test_invalid_save_does_not_create_record(self):
        for payload in [{'title': ''}, {'title': '有效', 'options': {'cutoff': False}}, {'title': '有效', 'unexpected': 1}]:
            request = Request(self.base + '/api/analyses', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.get('/api/analyses'), [])
