const statusNode = document.querySelector('#status');
const filters = document.querySelector('#filters');
let current = null;
let requestGeneration = 0;
let catalog = null;
let modelData = null;
let comparedScenarios = [];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = typeof value === 'object' ? JSON.stringify(value) : String(value ?? '未知');
  return node;
}

function table(container, rows) {
  container.replaceChildren();
  if (!rows.length) {
    container.append(text('p', '当前范围没有记录'));
    return;
  }
  const columns = Array.from(new Set(rows.flatMap(row => Object.keys(row))));
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  for (const column of columns) head.append(text('th', column));
  const thead = document.createElement('thead');
  thead.append(head);
  grid.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const line = document.createElement('tr');
    for (const column of columns) line.append(text('td', row[column]));
    body.append(line);
  }
  grid.append(body);
  container.append(grid);
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits, minimumFractionDigits: digits});
}

function interval(node, stats, digits = 2) {
  node.textContent = `${fmt(stats.point, digits)}（90% 区间 ${fmt(stats.p05, digits)} ~ ${fmt(stats.p95, digits)}）`;
}

function render(value, savedTitle = '') {
  current = value;
  const summary = document.querySelector('#summary');
  summary.replaceChildren();
  for (const [key, amount] of Object.entries(value.result.summary)) {
    summary.append(text('dt', key), text('dd', amount));
  }
  document.querySelector('#notes').replaceChildren(...value.result.notes.map(note => text('li', note)));
  document.querySelector('#digest').textContent = `资料指纹：${value.digest}`;
  table(document.querySelector('#results'), value.result.rows);
  statusNode.textContent = savedTitle ? `正在查看保存的分析：${savedTitle}` : '分析已更新';
  filters.elements.cutoff.value = value.options.cutoff;
  filters.elements.group.value = value.options.group;
}

async function refresh() {
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(filters));
  statusNode.textContent = '正在分析';
  try {
    const result = await api(`/api/view?${query}`);
    if (generation === requestGeneration) render(result);
    await loadModel(query, generation);
  } catch (error) {
    if (generation === requestGeneration) {
      current = null;
      statusNode.textContent = error.message;
    }
  }
}

async function loadModel(query, generation) {
  const [model, validation] = await Promise.all([
    api(`/api/model?${query}`),
    api(`/api/validation?${query}`),
  ]);
  if (generation !== requestGeneration) return;
  modelData = model;
  renderModel(model.model);
  renderValidation(validation.validation);
  buildScenarioForm(model.model);
}

function renderModel(model) {
  const metrics = document.querySelector('#model-metrics');
  metrics.replaceChildren();
  const cards = [
    ['价格系数 β(log 价格)', fmt(model.beta_price, 3), `标准误 ${fmt(model.beta_price_se, 3)}`],
    ['McFadden R²', fmt(model.mc_fadden_r2, 3), `对数似然 ${fmt(model.log_likelihood, 1)}`],
    ['岭先验强度', fmt(model.ridge_beta, 1), '抑制价格项过拟合'],
    ['价格效应识别', model.diagnostics.price_effect_weak ? '弱' : '可辨', model.diagnostics.price_coefficient_sign],
  ];
  for (const [title, main, sub] of cards) {
    const card = document.createElement('div');
    card.className = 'card';
    card.append(text('h3', title), text('p', main), text('small', sub));
    metrics.append(card);
  }
  const notes = document.querySelector('#model-notes');
  notes.replaceChildren(
    text('li', '不购买是一次选择：choice 为空按外部选项建模；库存不可售商品不进入当次选择集，不使用售出占比反推偏好。'),
    text('li', model.diagnostics.note_nesting),
    text('li', model.diagnostics.note_price),
    text('li', '定价与陈列并非随机实验，模型给出的是观测数据内的条件相关与结构反事实，不是因果弹性；跨店差异由门店不购买项吸收，未估计门店×商品交互。'),
  );
  renderSupport(model.support);
}

function renderSupport(support) {
  const container = document.querySelector('#support');
  const items = support.items;
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  head.append(text('th', '商品对'), text('th', '共同可选次数'), text('th', '可选到店'), text('th', '被选次数'), text('th', '出现门店'));
  grid.append(head);
  const body = document.createElement('tbody');
  for (const [pair, count] of Object.entries(support.co_available)) {
    const line = document.createElement('tr');
    if (count === 0) line.className = 'unsupported';
    line.append(text('td', pair), text('td', count),
      text('td', pair.split('|').map(i => support.available_visits[i]).join(' / ')),
      text('td', pair.split('|').map(i => support.chosen_visits[i]).join(' / ')),
      text('td', pair.split('|').map(i => support.stores[i].join('、')).join(' / ')));
    body.append(line);
  }
  grid.append(body);
  container.replaceChildren(grid);
}

function renderValidation(validation) {
  const container = document.querySelector('#validation');
  const cv = validation.cross_validation;
  const th = validation.temporal_holdout;
  const wrap = document.createElement('div');
  const cvTable = metricTable(cv.summary, cv.method);
  const thTable = metricTable(th.metrics, `${th.method}（切分日 ${th.split_day}）`);
  const leak = document.createElement('ul');
  for (const guarantee of validation.leakage_guarantees) leak.append(text('li', guarantee));
  const leakStatus = text('p', `顾客泄漏折数：${cv.customer_leakage.length}；时间泄漏记录：${th.future_display_leakage.length}`);
  wrap.append(text('h4', '分组交叉验证'), cvTable, text('h4', '时间外推'), thTable, leakStatus, leak);
  container.replaceChildren(wrap);
}

function metricTable(summary, caption) {
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  head.append(text('th', '模型/基准'), text('th', '平均对数损失（越低越好）'), text('th', '命中率'));
  grid.append(head);
  const labels = {choice_model: '选择模型（MNL+价格+门店项）', uniform: '等概率基准', intercept_only: '仅截距基准'};
  const body = document.createElement('tbody');
  for (const [key, values] of Object.entries(summary)) {
    const line = document.createElement('tr');
    const loss = values.mean_log_loss ?? values.log_loss;
    const hit = values.mean_hit_rate ?? values.hit_rate;
    line.append(text('td', labels[key] || key), text('td', fmt(loss, 4)), text('td', fmt(hit, 4)));
    body.append(line);
  }
  grid.append(body);
  const wrap = document.createElement('figure');
  wrap.append(text('figcaption', caption), grid);
  return wrap;
}

// ---------------- 情景试算 ---------------- #
function buildScenarioForm(model) {
  const support = model.support;
  const removalBox = document.querySelector('#scenario-removals');
  removalBox.replaceChildren();
  for (const item of support.items) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.name = 'removal'; input.value = item;
    label.append(input, text('span', `${item}（可选 ${support.available_visits[item]} 次）`));
    removalBox.append(label);
  }
  const priceBox = document.querySelector('#scenario-prices');
  priceBox.replaceChildren();
  for (const item of support.items) {
    const label = document.createElement('label');
    label.append(text('span', `${item} 新价格`));
    const input = document.createElement('input');
    input.type = 'number'; input.step = '0.01'; input.min = '0';
    input.name = 'price'; input.dataset.item = item; input.placeholder = `历史档位 ${support.price_levels[item].join('/')}`;
    label.append(input);
    priceBox.append(label);
  }
  const additionBox = document.querySelector('#scenario-additions');
  additionBox.replaceChildren();
  addAdditionRow();
  document.querySelector('#add-row').onclick = addAdditionRow;
}

function addAdditionRow() {
  const box = document.querySelector('#scenario-additions');
  const row = document.createElement('div');
  row.className = 'addition-row';
  const fields = [['item', '商品标识', 'text'], ['family', '品类 family', 'text'], ['size', '规格 size', 'number'], ['price', '展示价格', 'number']];
  for (const [name, placeholder, type] of fields) {
    const input = document.createElement('input');
    input.type = type; input.dataset.field = name; input.placeholder = placeholder;
    if (name === 'size' || name === 'price') input.step = 'any';
    row.append(input);
  }
  const remove = document.createElement('button');
  remove.type = 'button'; remove.textContent = '删除';
  remove.onclick = () => row.remove();
  row.append(remove);
  box.append(row);
}

function collectScenario() {
  const removals = [...document.querySelectorAll('input[name=removal]:checked')].map(i => i.value);
  const price_changes = {};
  for (const input of document.querySelectorAll('#scenario-prices input')) {
    if (input.value !== '') price_changes[input.dataset.item] = Number(input.value);
  }
  const additions = [];
  for (const row of document.querySelectorAll('.addition-row')) {
    const record = {};
    let empty = true;
    for (const input of row.querySelectorAll('input')) {
      if (input.value !== '') empty = false;
      record[input.dataset.field] = input.dataset.field === 'size' || input.dataset.field === 'price'
        ? Number(input.value) : input.value;
    }
    if (!empty) additions.push(record);
  }
  return {removals, price_changes, additions};
}

document.querySelector('#scenario-form').addEventListener('submit', async event => {
  event.preventDefault();
  const scenarioStatus = document.querySelector('#scenario-status');
  scenarioStatus.textContent = '正在试算';
  try {
    const query = new URLSearchParams(new FormData(filters));
    const scope = Object.fromEntries(query);
    const payload = {options: scope, scenario: collectScenario()};
    const response = await api('/api/scenario', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    renderScenario(response);
    scenarioStatus.textContent = '情景试算完成';
  } catch (error) {
    scenarioStatus.textContent = error.message;
  }
});

function renderScenario(response) {
  const result = response.result;
  const root = document.querySelector('#scenario-result');
  root.replaceChildren();

  const headline = document.createElement('div');
  headline.className = 'cards';
  const revenueCard = document.createElement('div');
  revenueCard.className = 'card';
  revenueCard.append(text('h3', '门店经营额（情景 vs 基线）'));
  const p = document.createElement('p');
  interval(p, result.counterfactual.expected_revenue);
  const delta = document.createElement('p');
  delta.className = result.revenue_delta.point < 0 ? 'negative' : 'positive';
  delta.textContent = `经营额变化：`;
  const deltaValue = text('span', '');
  interval(deltaValue, result.revenue_delta);
  delta.append(deltaValue);
  const base = document.createElement('small');
  base.textContent = `基线期望经营额：${fmt(result.baseline.expected_revenue.point)}`;
  revenueCard.append(p, delta, base);
  const nonpurchaseCard = document.createElement('div');
  nonpurchaseCard.className = 'card';
  nonpurchaseCard.append(text('h3', '期望不购买人次'));
  const np = document.createElement('p');
  interval(np, result.counterfactual.expected_nonpurchase, 1);
  const npBase = document.createElement('small');
  npBase.textContent = `基线 ${fmt(result.baseline.expected_nonpurchase.point, 1)} 人次`;
  nonpurchaseCard.append(np, npBase);
  headline.append(revenueCard, nonpurchaseCard);
  root.append(headline);

  if (Object.keys(result.diversion).length) {
    root.append(text('h3', '撤下商品的销量去向（三类）'));
    for (const [removed, detail] of Object.entries(result.diversion)) {
      root.append(text('h4', `撤下 ${removed}`));
      const grid = document.createElement('table');
      const head = document.createElement('tr');
      head.append(text('th', '去向'), text('th', '承接商品'), text('th', '期望销量（90% 区间）'), text('th', '共同可选次数'));
      grid.append(head);
      const body = document.createElement('tbody');
      for (const [item, stats] of Object.entries(detail.to_items_supported)) {
        const line = document.createElement('tr');
        const cell = document.createElement('td');
        cell.textContent = '同店商品（有观测支撑）';
        const statCell = document.createElement('td');
        interval(statCell, stats, 2);
        line.append(cell, text('td', item), statCell, text('td', detail.co_available[item]));
        body.append(line);
      }
      for (const [item, stats] of Object.entries(detail.to_items_unsupported)) {
        const line = document.createElement('tr');
        line.className = 'unsupported';
        const statCell = document.createElement('td');
        interval(statCell, stats, 2);
        line.append(text('td', '无观测支撑的替代（IIA 外推）'), text('td', item), statCell, text('td', detail.co_available[item]));
        body.append(line);
      }
      const outsideLine = document.createElement('tr');
      const outsideCell = document.createElement('td');
      interval(outsideCell, detail.to_outside, 2);
      outsideLine.append(text('td', '直接不买'), text('td', '外部选项'), outsideCell, text('td', '—'));
      body.append(outsideLine);
      grid.append(body);
      root.append(grid);
    }
  }

  root.append(text('h3', '商品蚕食与销量变化（点估计）'));
  const cannTable = document.createElement('table');
  const cannHead = document.createElement('tr');
  cannHead.append(text('th', '商品'), text('th', '状态'), text('th', '基线销量'), text('th', '情景销量'), text('th', '销量变化'));
  cannTable.append(cannHead);
  const cannBody = document.createElement('tbody');
  for (const row of result.cannibalization.items) {
    const line = document.createElement('tr');
    line.append(text('td', row.item), text('td', row.status),
      text('td', fmt(row.baseline_units, 2)), text('td', fmt(row.scenario_units, 2)),
      text('td', fmt(row.unit_delta, 2)));
    cannBody.append(line);
  }
  cannTable.append(cannBody);
  root.append(cannTable);
  const gained = document.createElement('p');
  gained.textContent = `留存/上新/调价商品净吸纳销量：${fmt(result.cannibalization.gained_units, 2)} 件（含对其他商品的蚕食与不购买回流）。`;
  root.append(gained);

  if (result.support_flags.length) {
    root.append(text('h3', '无观测支撑提示'));
    const flagList = document.createElement('ul');
    for (const flag of result.support_flags) {
      const label = flag.removed ? `撤下 ${flag.removed} → ${flag.candidate}` : `上新 ${flag.candidate}`;
      flagList.append(text('li', `${label}：${flag.reason}`));
    }
    root.append(flagList);
  }

  comparedScenarios.push({label: scenarioLabel(result.scenario), revenue: result.counterfactual.expected_revenue,
    delta: result.revenue_delta, nonpurchase: result.counterfactual.expected_nonpurchase});
  renderComparison();
}

function scenarioLabel(scenario) {
  const parts = [];
  if (scenario.removals.length) parts.push(`撤下 ${scenario.removals.join('、')}`);
  if (Object.keys(scenario.price_changes).length) parts.push(`调价 ${Object.entries(scenario.price_changes).map(([i, p]) => `${i}=${p}`).join('，')}`);
  if (scenario.additions.length) parts.push(`上新 ${scenario.additions.map(a => a.item).join('、')}`);
  return parts.join('；') || '空情景（基线）';
}

function renderComparison() {
  const container = document.querySelector('#scenario-compare');
  if (!comparedScenarios.length) {
    container.replaceChildren();
    return;
  }
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  head.append(text('th', '情景'), text('th', '期望经营额'), text('th', '经营额区间'), text('th', '经营额变化'), text('th', '期望不购买'));
  grid.append(head);
  const body = document.createElement('tbody');
  for (const entry of comparedScenarios) {
    const line = document.createElement('tr');
    line.append(text('td', entry.label), text('td', fmt(entry.revenue.point)),
      text('td', `${fmt(entry.revenue.p05)} ~ ${fmt(entry.revenue.p95)}`),
      text('td', fmt(entry.delta.point)), text('td', fmt(entry.nonpurchase.point, 1)));
    body.append(line);
  }
  grid.append(body);
  const clear = document.createElement('button');
  clear.type = 'button'; clear.textContent = '清空对比';
  clear.onclick = () => { comparedScenarios = []; renderComparison(); };
  container.replaceChildren(text('h3', '情景对比'), grid, clear);
}

async function history() {
  const entries = await api('/api/analyses');
  const list = document.querySelector('#history');
  list.replaceChildren();
  for (const entry of entries) {
    const item = document.createElement('li');
    const button = text('button', entry.title);
    button.type = 'button';
    button.addEventListener('click', async () => {
      const generation = ++requestGeneration;
      try {
        const saved = await api(`/api/analyses/${entry.id}`);
        if (generation === requestGeneration) render(saved, saved.title);
      } catch (error) {
        statusNode.textContent = error.message;
      }
    });
    item.append(button, text('small', entry.created_at));
    list.append(item);
  }
}

filters.addEventListener('submit', event => {
  event.preventDefault();
  refresh();
});

document.querySelector('#save').addEventListener('submit', async event => {
  event.preventDefault();
  if (!current) return;
  try {
    const saved = await api('/api/analyses', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: event.target.elements.title.value, options: current.options})});
    render(saved, saved.title);
    await history();
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

document.querySelector('#source').addEventListener('click', async () => {
  try {
    const data = await api(`/api/source?table=${encodeURIComponent(document.querySelector('#tables').value)}`);
    table(document.querySelector('#raw'), data.rows);
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

async function start() {
  catalog = await api('/api/catalog');
  document.querySelector('#description').textContent = catalog.description;
  filters.elements.cutoff.min = catalog.min_cutoff;
  filters.elements.cutoff.max = catalog.max_cutoff;
  filters.elements.cutoff.value = catalog.default_cutoff;
  const groups = [['', '全部'], ...catalog.groups.map(group => [group, group])];
  for (const [key, label] of groups) {
    const option = text('option', label);
    option.value = key;
    filters.elements.group.append(option);
  }
  for (const name of Object.keys(catalog.tables)) {
    const option = text('option', `${name}（${catalog.tables[name]}）`);
    option.value = name;
    document.querySelector('#tables').append(option);
  }
  await refresh();
  await history();
}

start().catch(error => { statusNode.textContent = error.message; });
