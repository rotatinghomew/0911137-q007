import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'choice_lab', '--port', str(port), '--state', str(Path(directory) / 'state.sqlite3')], cwd=ROOT)
        base = f'http://127.0.0.1:{port}'
        try:
            for attempt in range(300):
                try:
                    with urlopen(base + '/api/catalog', timeout=1) as response:
                        catalog = json.load(response)
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('服务启动超时')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")
                assert page.locator('#results tbody tr').count() > 0
                page.locator('#filters select').select_option(catalog['groups'][0])
                page.get_by_role('button', name='更新分析', exact=True).click()
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")

                # 选择模型与检验结果渲染。
                page.wait_for_selector('#model-metrics .card')
                assert page.locator('#support tbody tr.unsupported').count() > 0
                assert page.locator('#validation table').count() >= 2

                # 情景试算：撤下从未共同可选的 i5，验证三类去向与对比表。
                # i5 只在乙店经营，先切回全部范围，保证撤下动作有可归因的需求。
                page.locator('#filters select').select_option('')
                page.get_by_role('button', name='更新分析', exact=True).click()
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")
                page.wait_for_selector('#model-metrics .card')
                page.locator('#scenario-removals input[value="i5"]').check()
                page.get_by_role('button', name='运行情景试算', exact=True).click()
                page.wait_for_function("document.querySelector('#scenario-status').textContent === '情景试算完成'")
                page.wait_for_function("document.querySelectorAll('#scenario-result tbody tr.unsupported').length >= 3")
                assert page.locator('#scenario-compare tbody tr').count() == 1

                # 第二个情景进入对比。
                page.locator('#scenario-removals input[value="i5"]').uncheck()
                price_input = page.locator('#scenario-prices input[data-item="i1"]')
                price_input.fill('10')
                page.get_by_role('button', name='运行情景试算', exact=True).click()
                page.wait_for_function("document.querySelector('#scenario-status').textContent === '情景试算完成'")
                page.wait_for_function("document.querySelectorAll('#scenario-compare tbody tr').length === 2")

                # 校验错误能在页面反馈（撤下与调价冲突）。
                page.locator('#scenario-removals input[value="i2"]').check()
                bad_price = page.locator('#scenario-prices input[data-item="i2"]')
                bad_price.fill('20')
                page.get_by_role('button', name='运行情景试算', exact=True).click()
                page.wait_for_function("document.querySelector('#scenario-status').textContent.includes('已撤下')")
                bad_price.fill('')
                page.locator('#scenario-removals input[value="i2"]').uncheck()

                # 恢复到单一业务范围后再保存，验证保存与回看沿用筛选条件。
                page.locator('#filters select').select_option(catalog['groups'][0])
                page.get_by_role('button', name='更新分析', exact=True).click()
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")
                page.locator('#save input').fill('浏览器业务核对')
                page.get_by_role('button', name='保存当前分析').click()
                page.wait_for_selector('#history button')
                page.reload()
                page.wait_for_selector('#history button')
                page.locator('#history button').click()
                page.wait_for_function("document.querySelector('#status').textContent.includes('浏览器业务核对')")
                assert page.locator('#filters select').input_value() == catalog['groups'][0]
                page.get_by_role('button', name='查看资料').click()
                page.wait_for_selector('#raw tbody tr')
                assert not errors, errors
                print(json.dumps({'结果': '通过', '页面错误': errors, '保存范围': page.locator('#filters select').input_value(),
                                  '情景对比行数': page.locator('#scenario-compare tbody tr').count()}, ensure_ascii=False))
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
