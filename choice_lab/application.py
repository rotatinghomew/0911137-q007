import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .domain import run
from .storage import AnalysisStore, read_dataset


ROOT = Path(__file__).resolve().parents[1]


class Application:
    def __init__(self, dataset, database, assets):
        self.dataset = Path(dataset)
        self.store = AnalysisStore(database)
        self.assets = Path(assets)

    def catalog(self):
        data, digest = read_dataset(self.dataset)
        return {**data['meta'], 'digest': digest, 'tables': {name: len(rows) for name, rows in data['tables'].items()}}

    def analyze(self, supplied):
        if not isinstance(supplied, dict) or set(supplied) - {'cutoff', 'group'}:
            raise ValueError('只接受 cutoff 和 group 筛选项')
        data, digest = read_dataset(self.dataset)
        cutoff = supplied.get('cutoff', data['meta']['default_cutoff'])
        try:
            if isinstance(cutoff, bool) or float(cutoff) != int(cutoff):
                raise ValueError()
            cutoff = int(cutoff)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('分析时点须为整数') from None
        group = supplied.get('group', '')
        if not isinstance(group, str) or group not in ['', *data['meta']['groups']]:
            raise ValueError('未知分析范围')
        if not data['meta']['min_cutoff'] <= cutoff <= data['meta']['max_cutoff']:
            raise ValueError('分析时点超出资料范围')
        options = {'cutoff': cutoff, 'group': group}
        result = run(data['tables'], options)
        return {'digest': digest, 'options': options, 'result': result}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, application, **kwargs):
        self.application = application
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        request = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(request.query, keep_blank_values=True).items()}
        try:
            if request.path == '/api/catalog':
                value = self.application.catalog()
            elif request.path == '/api/view':
                value = self.application.analyze(query)
            elif request.path == '/api/analyses':
                value = self.application.store.list()
            elif request.path.startswith('/api/analyses/'):
                value = self.application.store.get(request.path.rsplit('/', 1)[-1])
            elif request.path == '/api/source':
                data, digest = read_dataset(self.application.dataset)
                table = query.get('table', '')
                if table not in data['tables']:
                    raise KeyError('资料表不存在')
                value = {'digest': digest, 'rows': data['tables'][table]}
            elif request.path in {'/', '/app.js', '/style.css'}:
                name = 'index.html' if request.path == '/' else request.path[1:]
                payload = (self.application.assets / name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name] + '; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            else:
                raise KeyError('路径不存在')
            self.send_json(200, value)
        except KeyError as error:
            self.send_json(404, {'error': str(error)})
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error': str(error)})

    def do_POST(self):
        if urlsplit(self.path).path != '/api/analyses':
            self.send_json(404, {'error': '路径不存在'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('请求大小不合适')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) - {'title', 'options'}:
                raise ValueError('保存请求字段无效')
            analyzed = self.application.analyze(payload.get('options', {}))
            value = self.application.store.save(payload.get('title'), analyzed['digest'], analyzed['options'], analyzed['result'])
            self.send_json(201, value)
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error': str(error)})


def server(application, host='127.0.0.1', port=0):
    return ThreadingHTTPServer((host, port), partial(Handler, application=application))


def main():
    parser = argparse.ArgumentParser(description='启动本地分析工作台')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8080, type=int)
    parser.add_argument('--data', type=Path, default=ROOT / 'fixtures/data.json')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('APP_STATE', '.state/analyses.sqlite3')))
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    application = Application(args.data, args.state, ROOT / 'web')
    httpd = server(application, args.host, 0 if args.demo else args.port)
    if args.demo:
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        base = f'http://{args.host}:{httpd.server_port}'
        try:
            with urlopen(base + '/api/view') as response:
                value = json.load(response)
            request = Request(base + '/api/analyses', data=json.dumps({'title': '当前资料摘要', 'options': value['options']}).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                saved = json.load(response)
            with urlopen(base + '/api/analyses/' + saved['id']) as response:
                restored = json.load(response)
            print(json.dumps({'当前摘要': value['result']['summary'], '资料指纹': value['digest'], '保存后相同': restored['result'] == value['result'], '说明': value['result']['notes']}, ensure_ascii=False, indent=2))
        finally:
            httpd.shutdown()
            worker.join()
            httpd.server_close()
    else:
        print(f'本地工作台：http://{args.host}:{httpd.server_port}', flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
