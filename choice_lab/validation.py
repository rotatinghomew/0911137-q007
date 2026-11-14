"""模型检验：分组交叉验证、时间外推与泄漏防护。

- 交叉验证按 customer 整组划分，同一顾客的多次到店不会同时出现在训练集和测试集，
  防止把同一个体的偏好重复计入训练与评估。
- 时间外推只用前一段时期训练、后一段时期评估，模拟“用过去预测未来”；
  建模与评估都只使用当次及之前 shown_at 的陈列信息，未来陈列不进入选择集。
- 对照基准：(1) 等概率基准（选择集内均分 + 不购买等权）；(2) 忽略价格与门店的
  仅截距 MNL。报告对数损失与命中率，验证选择模型是否真的优于朴素口径，
  而不是用售出占比反推偏好。
"""
from collections import defaultdict
import math

from .choice_model import (
    MultinomialLogit, build_observations, fit_model, ChoiceDataError,
)


def _log_loss_and_hit(observations, predictor):
    """predictor(observation) -> (商品->概率, 不购买概率)。"""
    total_log_loss = 0.0
    hits = 0
    for observation in observations:
        probabilities, outside_prob = predictor(observation)
        probability = outside_prob if observation['choice'] is None \
            else probabilities.get(observation['choice'], 0.0)
        total_log_loss += math.log(max(probability, 1e-12))
        if observation['choice'] is not None and probabilities:
            predicted = max(probabilities, key=probabilities.get)
            if predicted == observation['choice']:
                hits += 1
    n = len(observations)
    return {
        'log_loss': -total_log_loss / n if n else None,
        'hit_rate': hits / n if n else None,
        'n': n,
    }


def uniform_predictor(observation):
    """等概率基准：选择集商品与不购买等权。"""
    choices = observation['choices']
    weight = 1.0 / (len(choices) + 1)
    return ({item: weight for item in choices}, weight)


def _intercept_only_model(train_observations, items):
    """仅商品/外部截距、无价格、无门店差异的 MNL（简化对照）。"""
    model = MultinomialLogit(train_observations, items)

    def predictor(observation):
        # 用训练集边际选择份额（含不购买）做平滑估计。
        counts = defaultdict(float)
        for row in train_observations:
            counts[row['choice'] or 'none'] += 1.0
        total = len(train_observations) + len(model.item_ids) + 1
        def smoothed(key):
            return (counts[key] + 1.0) / total
        outside = smoothed('none')
        raw = {item: smoothed(item) for item in observation['choices']}
        scale = (1.0 - outside) / (sum(raw.values()) or 1.0)
        return ({item: value * scale for item, value in raw.items()}, outside)

    return predictor


def grouped_folds(observations, k=5, seed=7):
    """按 customer 确定性分组，返回 k 个 (训练集, 测试集)。"""
    customers = sorted({observation['customer'] for observation in observations})
    if len(customers) < k:
        raise ChoiceDataError('顾客数少于折数，无法做分组交叉验证')
    # 确定性洗牌（不依赖随机库状态）。
    multiplier = (seed * 2654435761) & 0xFFFFFFFF
    customers.sort(key=lambda customer: (hash_fun(customer, multiplier), customer))
    folds = [[] for _ in range(k)]
    for index, customer in enumerate(customers):
        folds[index % k].append(customer)
    groups = [set(fold) for fold in folds]
    result = []
    for index in range(k):
        held = groups[index]
        test = [observation for observation in observations
                if observation['customer'] in held]
        train = [observation for observation in observations
                 if observation['customer'] not in held]
        result.append((train, test))
    return result


def hash_fun(value, multiplier):
    total = 0
    for char in value:
        total = (total * 31 + ord(char)) & 0xFFFFFFFF
    return (total * multiplier) & 0xFFFFFFFF


def cross_validate(tables, cutoff, group='', k=5):
    observations, items = build_observations(tables, cutoff, group)
    folds = grouped_folds(observations, k)
    fold_reports = []
    aggregate = defaultdict(lambda: defaultdict(list))
    for index, (train, test) in enumerate(folds):
        fitted = fit_model(train, items)
        model = fitted['model']
        params = fitted['params']
        choice_predictor = lambda observation, model=model, params=params: \
            model.probabilities(params, observation)
        metrics = {
            'choice_model': _log_loss_and_hit(test, choice_predictor),
            'uniform': _log_loss_and_hit(test, uniform_predictor),
            'intercept_only': _log_loss_and_hit(test, _intercept_only_model(train, items)),
        }
        fold_reports.append({'fold': index, 'train_n': len(train),
                             'test_n': len(test), 'metrics': metrics})
        for name, values in metrics.items():
            aggregate[name]['log_loss'].append(values['log_loss'])
            aggregate[name]['hit_rate'].append(values['hit_rate'])
    summary = {}
    for name, values in aggregate.items():
        summary[name] = {
            'mean_log_loss': sum(values['log_loss']) / len(values['log_loss']),
            'mean_hit_rate': sum(values['hit_rate']) / len(values['hit_rate']),
        }
    # 泄漏自检：各折训练/测试顾客必须互不相交。
    leakage = []
    for index, (train, test) in enumerate(folds):
        train_customers = {o['customer'] for o in train}
        test_customers = {o['customer'] for o in test}
        overlap = train_customers & test_customers
        if overlap:
            leakage.append({'fold': index, 'overlap_customers': sorted(overlap)})
    return {
        'method': f'{k} 折按顾客分组交叉验证（同一顾客不跨训练/测试）',
        'folds': fold_reports,
        'summary': summary,
        'customer_leakage': leakage,
    }


def temporal_holdout(tables, cutoff, group='', train_fraction=0.7):
    """时间外推：早期训练、晚期评估，检验对未来陈列的预测力。"""
    observations, items = build_observations(tables, cutoff, group)
    ordered = sorted(observations, key=lambda row: (row['day'], row['visit']))
    split_index = int(len(ordered) * train_fraction)
    train, test = ordered[:split_index], ordered[split_index:]
    if not train or not test:
        raise ChoiceDataError('时间切分后训练集或测试集为空')
    split_day = train[-1]['day']
    # 泄漏自检：测试集不应早于训练末日；训练观测不含 split_day 之后的陈列。
    future_leaks = [observation['visit'] for observation in train
                    if observation['day'] > split_day]
    fitted = fit_model(train, items)
    model = fitted['model']
    params = fitted['params']
    choice_predictor = lambda observation, model=model, params=params: \
        model.probabilities(params, observation)
    return {
        'method': '时间外推（按到店日排序，前 70% 训练、后 30% 评估）',
        'split_day': split_day,
        'train_n': len(train),
        'test_n': len(test),
        'metrics': {
            'choice_model': _log_loss_and_hit(test, choice_predictor),
            'uniform': _log_loss_and_hit(test, uniform_predictor),
            'intercept_only': _log_loss_and_hit(test, _intercept_only_model(train, items)),
        },
        'future_display_leakage': future_leaks,
    }


def validation_report(tables, cutoff, group='', k=5):
    return {
        'cross_validation': cross_validate(tables, cutoff, group, k),
        'temporal_holdout': temporal_holdout(tables, cutoff, group),
        'leakage_guarantees': [
            '交叉验证按 customer 整组划分，同一顾客的多次到店不会同时进入训练集与测试集。',
            '所有观测只使用 shown_at 不晚于到店日、day 不晚于分析时点的陈列信息。',
            'available=false 的库存不可售商品不进入当次选择集。',
            '时间外推严格用早期到店训练、晚期到店评估，不使用未来陈列信息。',
        ],
    }
