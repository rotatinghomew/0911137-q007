# 门店选品试验室

用于查阅门店选品试验室的业务记录、当前摘要与保存的分析版本，并在到店选择数据上估计可检验的选择模型，支持商品退出、上新与调价的组合情景试算。现有功能包括业务范围筛选、历史时点查阅、原始资料展开、选择模型与检验、情景去向三分与经营额不确定性、不可变分析结果回看。统计口径见 `docs/data-contract.md`，HTTP 结构见 `docs/http-api.md`。

## 选择模型与情景试算

- 每次到店是一次选择，`choice=null` 按“不购买”外部选项建模；库存不可售（`available=false`）和未来陈列（`shown_at` 晚于到店日）不进入当次选择集，不使用售出比例反推偏好。
- 主模型为多项式 Logit：商品效用含 ASC 与对数价格项，不购买效用含门店外部项；价格系数带岭先验。按 family 自由估计的嵌套结构因不可靠识别而不采用，诊断中如实说明。
- 情景试算把撤下商品的销量去向分为**同店商品（有共同可选支撑）、直接不买、无观测支撑的替代（IIA 外推）**三类，并给出门店经营额与商品蚕食的点估计和 90% 区间。从未共同可选的商品（如 i5 与 i1–i4）不会被硬给精确替代率。
- 模型检验使用按顾客分组的 K 折交叉验证（同一顾客不跨训练/测试）与时间外推（早期训练、晚期评估），并与等概率、仅截距基准对比；定价与陈列非随机，模型输出是条件相关与结构反事实而非因果弹性，识别局限在页面与接口中明确标注。

## 运行

需要 Python >=3.11,<3.12，不依赖特定补丁版本。应用运行只使用标准库，浏览器 JavaScript 无构建步骤。以下命令在项目根目录执行：

```sh
python3 -m choice_lab
```

浏览器打开本机 8080 端口即可操作。资料默认来自 `fixtures/data.json`；分析结果默认保存在 `.state/analyses.sqlite3`。可用 `--data` 指定同口径资料，`--state` 指定状态文件，`--port` 调整监听端口。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试涵盖领域口径、真实 HTTP 往返、错误筛选、跨进程存储及不可变保存。页面直接消费与测试相同的 API，不使用预录结果。

浏览器验证工具与产品运行依赖分开锁定；准备一次后可重复执行筛选、保存、刷新回看和资料查阅：

```sh
python3 -m pip install -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 tests/browser_smoke.py
```

前两条是首次测试环境安装，完成后无需重复执行。基础容器只运行产品和标准库测试；浏览器测试在安装了 `requirements-test.txt` 与 Chromium 的开发环境运行。

也可使用无第三方 JavaScript 依赖的 npm 工作区入口：`npm ci --ignore-scripts` 后，`npm start -- --host 127.0.0.1 --port 8080` 启动同一个 Python HTTP 服务，`npm run test:browser` 执行上述浏览器回归。npm 仅负责命令编排，不是产品运行依赖；不要把 `web/` 单独作为静态站点运行，页面需要同源 API。

## 容器

```sh
docker build -t choice-lab:local .
docker run --rm -p 127.0.0.1:8080:8080 choice-lab:local
```

基础镜像以内容摘要固定，Python 打包依赖固定在 `requirements-build.txt`。容器构建完成后可断网运行；若要保留容器外分析版本，可将宿主目录挂载到 `/app/.state`。

## 快速业务核对

```sh
python3 -m choice_lab --demo
```

此入口启动真实本地 HTTP 服务，计算当前摘要，保存后重新读取再退出。输出依赖当前资料，不预先指定统计结论。初次阅读可先在页面调整时点或业务范围，再保存和回看分析；测试和该核对入口可在九十秒内完成，不包含镜像构建时间。
