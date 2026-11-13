import copy
import json
from pathlib import Path
import tempfile
import unittest

from choice_lab.application import Application, ROOT
from choice_lab.storage import AnalysisStore, read_dataset


class StorageTests(unittest.TestCase):
    def test_fingerprint_depends_on_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'data.json'
            payload = (ROOT / 'fixtures/data.json').read_bytes()
            source.write_bytes(payload)
            original, digest = read_dataset(source)
            source.write_bytes(payload + b'\n')
            same, changed = read_dataset(source)
            self.assertEqual(original, same)
            self.assertNotEqual(digest, changed)

    def test_empty_store_and_unknown_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AnalysisStore(Path(directory) / 'data.sqlite3')
            self.assertEqual(store.list(), [])
            with self.assertRaises(KeyError):
                store.get('unknown')

    def test_nonfinite_result_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AnalysisStore(Path(directory) / 'data.sqlite3')
            with self.assertRaises(ValueError):
                store.save('无穷值', 'digest', {}, {'value': float('nan')})
            self.assertEqual(store.list(), [])

    def test_empty_scope_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'data.json'
            data = json.loads((ROOT / 'fixtures/data.json').read_text())
            data['tables'] = {key: [] for key in data['tables']}
            source.write_text(json.dumps(data))
            app = Application(source, Path(directory) / 'state.sqlite3', ROOT / 'web')
            result = app.analyze({})
            self.assertEqual(result['result']['rows'], [])
            self.assertTrue(result['result']['notes'])
