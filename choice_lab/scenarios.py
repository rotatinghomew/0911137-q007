"""情景试算引擎：商品退出、上新与调价的组合反事实。

去向三分口径：
- 转移到同店商品：撤下商品与承接商品在历史上“共同可选”过，替代有观测支撑；
- 直接不买：需求回流到外部选项；
- 没有观测支撑的替代：承接商品从未与撤下商品共同可选（如 i5 与 i1-i4），
  MNL 的 IIA 外推没有数据依据，只给模型外推参考并显式标注，不当作精确替代率。

不确定性来自 BHHH 参数抽样；新商品额外叠加层级先验方差。所有抽样固定种子。
"""
from collections import defaultdict
import math
import random

from .choice_model import (
    ChoiceDataError, DRAWS, SEED, sample_parameters, new_item_prior,
    _normal,
)


def _percentile(sorted_values, probability):
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _interval(values):
    ordered = sorted(values)
    return {
        'point': sum(values) / len(values),
        'p05': _percentile(ordered, 0.05),
        'p95': _percentile(ordered, 0.95),
    }


def validate_scenario(scenario, items):
    """校验并规范化情景定义。"""
    scenario = scenario or {}
    removals = scenario.get('removals', [])
    price_changes = scenario.get('price_changes', {})
    additions = scenario.get('additions', [])
    if not isinstance(removals, list) or not all(isinstance(x, str) for x in removals):
        raise ChoiceDataError('撤下商品必须是商品标识列表')
    if not isinstance(price_changes, dict):
        raise ChoiceDataError('调价必须是商品到价格的映射')
    removal_set = set(removals)
    catalog = set(items)
    unknown_removal = removal_set - catalog
    if unknown_removal:
        raise ChoiceDataError(f'撤下了资料中不存在的商品：{sorted(unknown_removal)}')
    for item, price in price_changes.items():
        if item in removal_set:
            raise ChoiceDataError(f'商品 {item} 已撤下，不能再调价')
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ChoiceDataError(f'商品 {item} 的情景价格必须为正的有限实数')
    normalized_additions = []
    seen = set()
    for addition in additions:
        if not isinstance(addition, dict) or {'item', 'family', 'size', 'price'} - set(addition):
            raise ChoiceDataError('上新必须包含 item、family、size、price')
        item = addition['item']
        if not isinstance(item, str) or not item:
            raise ChoiceDataError('上新商品标识无效')
        if item in seen:
            raise ChoiceDataError(f'上新商品 {item} 重复')
        seen.add(item)
        if item in catalog and item not in removal_set:
            raise ChoiceDataError(f'商品 {item} 已存在；上新标识不能与在售商品冲突')
        price = addition['price']
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ChoiceDataError(f'上新商品 {item} 的价格必须为正的有限实数')
        normalized_additions.append({
            'item': item, 'family': addition['family'],
            'size': addition['size'], 'price': float(price),
        })
    return {
        'removals': sorted(removal_set),
        'price_changes': {item: float(price) for item, price in price_changes.items()},
        'additions': normalized_additions,
    }


def _counterfactual_choices(observation, scenario, addition_ids):
    """把情景作用到一次到店的原始选择集，返回 (商品->情景价格, 新增商品集合)。"""
    choices = dict(observation['choices'])
    for item in scenario['removals']:
        choices.pop(item, None)
    for item, price in scenario['price_changes'].items():
        if item in choices:
            choices[item] = price
    added = {}
    for addition in addition_ids:
        choices[addition['item']] = addition['price']
        added[addition['item']] = addition
    return choices, added


def store_substitution_sets(fitted, observations, scenario):
    """每个门店的替代集：该店历史上经营过的商品（门店中位价格）+ 情景上新。

    用于把撤下商品的需求归到“同店经营但从未共同可选”的商品；这部分替代没有
    共同可选观测，会被标为无观测支撑，而不是被误归为直接不买。
    """
    model = fitted['model']
    prices_by_store = defaultdict(lambda: defaultdict(list))
    for observation in observations:
        store = observation['store']
        for item, price in observation['choices'].items():
            prices_by_store[store][item].append(price)
    additions = {a['item']: a for a in scenario['additions']}
    sets = {}
    for store, item_prices in prices_by_store.items():
        reference = {}
        for item, prices in item_prices.items():
            if item in scenario['removals']:
                continue
            ordered = sorted(prices)
            reference[item] = ordered[len(ordered) // 2]
        for item, addition in additions.items():
            reference[item] = addition['price']
        sets[store] = reference
    return sets


def _draw_utility_params(draw, model, additions, priors, rng):
    """把一次参数抽样扩展为含新商品 ASC 的效用参数。"""
    asc = {}
    for item in model.item_ids:
        asc[item] = draw[model.asc_index[item]] if item in model.asc_index else 0.0
    for addition in additions:
        prior = priors[addition['item']]
        # 新商品没有似然信息：先验均值 + 先验标准差的独立正态扰动。
        asc[addition['item']] = prior['prior_asc'] + prior['prior_sd'] * _normal(rng)
    return asc


def _probability_with(model, params, asc, choices, store):
    """给定（可能含新商品的）选择集计算 MNL 概率，复用模型的价格基准。"""
    beta = params[model.index_beta]
    outside_index = model.index_outside.get(store)
    outside_v = params[outside_index] if outside_index is not None else 0.0
    exponentials = {}
    for item, price in choices.items():
        exponentials[item] = math.exp(asc[item] + beta * math.log(price / model.mean_price))
    outside = math.exp(outside_v)
    denominator = outside + sum(exponentials.values())
    probabilities = {item: value / denominator for item, value in exponentials.items()}
    return probabilities, outside / denominator


def evaluate_scenario(fitted, observations, scenario, draws_count=DRAWS, seed=SEED):
    """计算基线与情景的期望销量、经营额区间，以及去向三分与蚕食。"""
    model = fitted['model']
    scenario = validate_scenario(scenario, model.items)
    additions = scenario['additions']
    priors = {addition['item']: new_item_prior(
        addition['item'], addition, fitted, observations) for addition in additions}
    draws = sample_parameters(fitted, draws_count, seed)
    rng = random.Random(seed + 1)

    item_universe = sorted(set(model.item_ids) | {a['item'] for a in additions})
    baseline_revenue_draws = []
    scenario_revenue_draws = []
    # 每次抽样下的期望销量，用于区间。
    baseline_units_draws = {item: [] for item in item_universe}
    scenario_units_draws = {item: [] for item in item_universe}
    baseline_outside_draws = []
    scenario_outside_draws = []
    # 撤下商品的去向：按承接商品累计（带支撑标记），逐抽样记录。
    diversion_draws = {removed: {'supported': defaultdict(list), 'unsupported': defaultdict(list),
                                 'outside': []}
                       for removed in scenario['removals']}

    # 支撑矩阵：撤下商品与各承接商品的共同可选次数。
    support = fitted['support']['co_available']
    substitution_sets = store_substitution_sets(fitted, observations, scenario)

    def pair_count(a, b):
        return support.get(f'{min(a, b)}|{max(a, b)}', 0)

    for draw in draws:
        asc = _draw_utility_params(draw, model, additions, priors, rng)
        baseline_units = {item: 0.0 for item in item_universe}
        scenario_units = {item: 0.0 for item in item_universe}
        baseline_revenue = 0.0
        scenario_revenue = 0.0
        baseline_outside = 0.0
        scenario_outside = 0.0
        diversion_step = {removed: {'supported': defaultdict(float), 'unsupported': defaultdict(float),
                                    'outside': 0.0}
                          for removed in scenario['removals']}
        for observation in observations:
            base_choices = observation['choices']
            base_prob, base_out = _probability_with(
                model, draw, asc, base_choices, observation['store'])
            for item, probability in base_prob.items():
                baseline_units[item] += probability
                baseline_revenue += probability * base_choices[item]
            baseline_outside += base_out

            scenario_choices, added = _counterfactual_choices(observation, scenario, additions)
            scen_prob, scen_out = _probability_with(
                model, draw, asc, scenario_choices, observation['store'])
            for item, probability in scen_prob.items():
                scenario_units[item] += probability
                scenario_revenue += probability * scenario_choices[item]
            scenario_outside += scen_out

            # 撤下商品的去向：在该门店的“替代集”上做条件反事实。
            # - 承接商品与被撤商品在历史上共同可选过，或属于本次上新：有观测/假设支撑；
            # - 同店经营但从未共同可选（如 i5 与 i1-i4）：IIA 外推，标无观测支撑；
            # - 外部选项：直接不买。
            for removed in scenario['removals']:
                removed_prob = base_prob.get(removed, 0.0)
                if removed_prob == 0.0:
                    continue
                store_set = dict(substitution_sets.get(observation['store'], {}))
                store_set.pop(removed, None)
                if not store_set:
                    diversion_step[removed]['outside'] += removed_prob
                    continue
                conditional_prob, conditional_out = _probability_with(
                    model, draw, asc, store_set, observation['store'])
                diversion_step[removed]['outside'] += removed_prob * conditional_out
                for item, probability in conditional_prob.items():
                    if item in added or pair_count(removed, item) > 0:
                        bucket = 'supported'
                    else:
                        bucket = 'unsupported'
                    diversion_step[removed][bucket][item] += removed_prob * probability
        baseline_revenue_draws.append(baseline_revenue)
        scenario_revenue_draws.append(scenario_revenue)
        baseline_outside_draws.append(baseline_outside)
        scenario_outside_draws.append(scenario_outside)
        for item in item_universe:
            baseline_units_draws[item].append(baseline_units[item])
            scenario_units_draws[item].append(scenario_units[item])
        for removed, detail in diversion_step.items():
            diversion_draws[removed]['outside'].append(detail['outside'])
            for item, value in detail['supported'].items():
                diversion_draws[removed]['supported'][item].append(value)
            for item, value in detail['unsupported'].items():
                diversion_draws[removed]['unsupported'][item].append(value)

    point_params = fitted['params']
    point_asc = dict(fitted['asc'])
    for addition in additions:
        point_asc[addition['item']] = priors[addition['item']]['prior_asc']

    def units_summary(units_draws):
        return {item: _interval(units_draws[item]) for item in item_universe
                if any(units_draws[item])}

    diversion = {}
    for removed, detail in diversion_draws.items():
        supported_items = {item: _interval(values)
                           for item, values in sorted(detail['supported'].items())}
        unsupported_items = {item: _interval(values)
                             for item, values in sorted(detail['unsupported'].items())}
        supported_total = [sum(detail['supported'][item][k] for item in detail['supported'])
                           for k in range(draws_count)]
        unsupported_total = [sum(detail['unsupported'][item][k] for item in detail['unsupported'])
                             for k in range(draws_count)]
        diversion[removed] = {
            'co_available': {item: pair_count(removed, item) for item in item_universe
                             if item != removed},
            'to_items_supported': supported_items,
            'to_items_unsupported': unsupported_items,
            'to_outside': _interval(detail['outside']),
            'supported_total': _interval(supported_total),
            'unsupported_total': _interval(unsupported_total),
        }

    # 蚕食：上新或留存商品从其他在售商品吸走的期望销量（情景下各商品相对基线的增量分解）。
    cannibalization = _cannibalization(
        fitted, observations, scenario, point_params, point_asc, priors)

    return {
        'scenario': scenario,
        'priors': priors,
        'draws': draws_count,
        'baseline': {
            'expected_units': units_summary(baseline_units_draws),
            'expected_nonpurchase': _interval(baseline_outside_draws),
            'expected_revenue': _interval(baseline_revenue_draws),
        },
        'counterfactual': {
            'expected_units': units_summary(scenario_units_draws),
            'expected_nonpurchase': _interval(scenario_outside_draws),
            'expected_revenue': _interval(scenario_revenue_draws),
        },
        'revenue_delta': _interval([s - b for s, b in
                                    zip(scenario_revenue_draws, baseline_revenue_draws)]),
        'diversion': diversion,
        'cannibalization': cannibalization,
        'support_flags': _support_flags(fitted, scenario, additions),
    }


def _cannibalization(fitted, observations, scenario, params, asc, priors):
    """点估计下分解每个承接商品对其他商品销量的净影响（单位：期望销量）。"""
    model = fitted['model']
    additions = scenario['additions']
    baseline_units = defaultdict(float)
    scenario_units = defaultdict(float)
    for observation in observations:
        base_prob, _ = _probability_with(
            model, params, asc, observation['choices'], observation['store'])
        for item, probability in base_prob.items():
            baseline_units[item] += probability
        scenario_choices, _ = _counterfactual_choices(observation, scenario, additions)
        scen_prob, _ = _probability_with(
            model, params, asc, scenario_choices, observation['store'])
        for item, probability in scen_prob.items():
            scenario_units[item] += probability
    rows = []
    for item in sorted(set(baseline_units) | set(scenario_units)):
        delta = scenario_units[item] - baseline_units.get(item, 0.0)
        if item in scenario['removals']:
            status = '撤下'
        elif any(a['item'] == item for a in additions):
            status = '上新'
        elif item in scenario['price_changes']:
            status = '调价'
        else:
            status = '留存'
        rows.append({
            'item': item, 'status': status,
            'baseline_units': baseline_units.get(item, 0.0),
            'scenario_units': scenario_units.get(item, 0.0),
            'unit_delta': delta,
        })
    # 蚕食总量：留存/上新商品中增量为正的部分，扣除来自不购买的回流后，视为对其他商品的蚕食。
    gained = sum(max(0.0, row['unit_delta']) for row in rows if row['status'] in {'上新', '留存', '调价'})
    return {'items': rows, 'gained_units': gained}


def _support_flags(fitted, scenario, additions):
    """标注情景中哪些预测缺乏共同可选观测支撑。"""
    support = fitted['support']['co_available']
    flags = []
    for removed in scenario['removals']:
        for item in fitted['model'].item_ids:
            if item == removed:
                continue
            count = support.get(f'{min(removed, item)}|{max(removed, item)}', 0)
            if count == 0:
                flags.append({
                    'removed': removed, 'candidate': item,
                    'reason': '两款商品从未在任一次到店中共同可选，替代率无观测支撑',
                })
    for addition in additions:
        flags.append({
            'removed': None, 'candidate': addition['item'],
            'reason': '新商品无历史选择观测，ASC 来自属性层级先验而非估计',
        })
    return flags
