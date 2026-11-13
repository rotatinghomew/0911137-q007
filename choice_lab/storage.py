import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def read_dataset(path):
    payload = Path(path).read_bytes()
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get('tables'), dict):
        raise ValueError('资料必须包含 tables 对象')
    for name, rows in data['tables'].items():
        if not isinstance(rows, list):
            raise ValueError(f'资料表 {name} 必须为列表')
    return data, hashlib.sha256(payload).hexdigest()


class AnalysisStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, digest TEXT NOT NULL, options TEXT NOT NULL, result TEXT NOT NULL)')

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, title, digest, options, result):
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError('分析名称须为一到一百个字符')
        identifier = uuid.uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute('INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?)', (identifier, title.strip(), created, digest, encode(options), encode(result)))
        return self.get(identifier)

    def get(self, identifier):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM analyses WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError('分析版本不存在')
        return {**dict(row), 'options': json.loads(row['options']), 'result': json.loads(row['result'])}

    def list(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT id,title,created_at,digest FROM analyses ORDER BY created_at DESC,id')]
