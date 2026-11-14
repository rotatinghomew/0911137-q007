"""可检验的离散选择模型与情景试算。

口径要点：
- 每次到店是一次选择；choice=None 表示选择了“不购买”这个外部选项，不是缺失。
- 当次选择集只包含 available=true 且 shown_at 不晚于到店日的商品；
  库存不可售的商品不进入选择集，也不能用售出比例反推偏好。
- 主模型为多项式 Logit（MNL）：商品效用 = ASC + beta*log(相对价格)，
  不购买效用含门店层面的外部选项项，用于刻画跨店不购买差异。
- 在当前资料上自由估计嵌套 Logit 的巢参数不可靠识别（巢参数与价格系数强混淆、
  似然在边界退化），数据不能拒绝 MNL；因此不把未经检验的嵌套结构当作结论，
  相似商品改由商品属性与“共同可选次数”支撑矩阵共同表示。
- 没有共同可选观测的商品对在结构上不可识别，情景结果单独归入“无观测支撑”，
  不硬给精确替代率。
- 全部计算只依赖标准库；随机抽样使用固定种子，保证结果可复现。
"""
from collections import defaultdict
import math
import random


# 新商品（没有任何到店观测）先验的宽度。这是约定的保守先验，不是估计值。
NEW_ITEM_PRIOR_SD = 1.5
# 价格系数的岭先验强度（对 beta*log(相对价格) 的二次惩罚）。
# 资料中价格档位少、与商品截距高度共线，弱先验用于抑制样本外过拟合；
# 交叉验证显示价格项的样本外增益有限，该局限会在诊断与文档中明确说明。
PRICE_RIDGE = 1.0
# 抽样数；固定后区间可复现。
DRAWS = 200
SEED = 20240517


class ChoiceDataError(ValueError):
    """资料口径不满足建模条件。"""


def _finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ChoiceDataError('价格必须为正的有限实数')
    return float(value)


def build_observations(tables, cutoff, group=''):
    """按分析时点与业务范围构造到店选择观测。

    每条包含 visit/customer/store/day/choice（None 表示不购买）/
    choices（选择集：商品 -> 当次实际价格）/outside_only（是否只能不购买）。
    只使用 cutoff 当天及之前、shown_at 不晚于到店日的信息，杜绝未来陈列泄漏。
    """
    visits = {row['visit']: row for row in tables['visits']
              if row['day'] <= cutoff and (not group or row['store'] == group)}
    offers = defaultdict(dict)
    for offer in tables['offers']:
        visit = visits.get(offer['visit'])
        if visit is not None and offer['shown_at'] <= visit['day']:
            offers[offer['visit']][offer['item']] = offer
    items = {row['item']: row for row in tables['items']}
    observations = []
    for key, visit in visits.items():
        choices = {}
        for item, offer in offers[key].items():
            if not offer['available']:
                continue
            if item not in items:
                raise ChoiceDataError(f'陈列商品 {item} 缺少 items 属性记录')
            if items[item]['launched_at'] > visit['day']:
                # 未上市商品不进入历史候选，属于资料口径错误而非自动跳过。
                raise ChoiceDataError(f'商品 {item} 在到店 {key} 时尚未上市')
            choices[item] = _finite_price(offer['price'])
        choice = visit['choice']
        if choice is not None and choice not in choices:
            raise ChoiceDataError(f'到店 {key} 的选择不在当次可选集内')
        observations.append({
            'visit': key, 'customer': visit['customer'], 'store': visit['store'],
            'day': visit['day'], 'choice': choice, 'choices': choices,
            'outside_only': not choices,
        })
    return observations, items


def support_matrix(observations, items):
    """统计商品共同可选次数、可选/被选次数、门店与价格档位。"""
    catalog = sorted(items)
    available = defaultdict(int)
    chosen = defaultdict(int)
    together = defaultdict(int)
    stores = defaultdict(set)
    price_levels = defaultdict(set)
    for observation in observations:
        present = sorted(observation['choices'])
        for item in present:
            available[item] += 1
            stores[item].add(observation['store'])
            price_levels[item].add(observation['choices'][item])
        if observation['choice'] is not None:
            chosen[observation['choice']] += 1
        for a, item in enumerate(present):
            for other in present[a + 1:]:
                together[tuple(sorted((item, other)))] += 1
    matrix = {}
    for a, item in enumerate(catalog):
        for other in catalog[a + 1:]:
            key = tuple(sorted((item, other)))
            matrix[f'{key[0]}|{key[1]}'] = together.get(key, 0)
    return {
        'items': catalog,
        'available_visits': {item: available[item] for item in catalog},
        'chosen_visits': {item: chosen[item] for item in catalog},
        'co_available': matrix,
        'stores': {item: sorted(stores[item]) for item in catalog},
        'price_levels': {item: sorted(price_levels[item]) for item in catalog},
    }


# --------------------------------------------------------------------------- #
# 多项式 Logit
# 效用：V_j = ASC_j + beta*log(price_j / 参考价)；不购买 V0 = ASC_0(store)。
# 识别约束：第一个商品 ASC=0。参数布局 ASC（其余商品）、beta、各门店外部项。
# --------------------------------------------------------------------------- #

class MultinomialLogit:
    def __init__(self, observations, items):
        self.observations = observations
        self.items = items
        self.item_ids = sorted(items)
        self.stores = sorted({observation['store'] for observation in observations})
        self.families = sorted({items[item]['family'] for item in self.item_ids})
        prices = [price for observation in observations
                  for price in observation['choices'].values()]
        self.mean_price = sum(prices) / len(prices) if prices else 1.0
        self.asc_index = {item: index for index, item in enumerate(self.item_ids[1:])}
        self.index_beta = len(self.asc_index)
        self.index_outside = {store: self.index_beta + 1 + index
                              for index, store in enumerate(self.stores)}
        self.dim = self.index_beta + 1 + len(self.stores)
        self.parameter_names = (
            [f'ASC:{item}' for item in self.item_ids[1:]]
            + ['beta:log_price']
            + [f'outside:{store}' for store in self.stores])

    def initial(self):
        params = [0.0] * self.dim
        params[self.index_beta] = -1.0
        return params

    def _utility(self, params, item, price):
        asc = params[self.asc_index[item]] if item in self.asc_index else 0.0
        return asc + params[self.index_beta] * math.log(price / self.mean_price)

    def _outside_utility(self, params, store):
        index = self.index_outside.get(store)
        return params[index] if index is not None else 0.0

    def probabilities(self, params, observation):
        """返回 (商品->概率, 不购买概率)，二者之和为 1。"""
        exponentials = {}
        for item, price in observation['choices'].items():
            exponentials[item] = math.exp(self._utility(params, item, price))
        outside = math.exp(self._outside_utility(params, observation['store']))
        denominator = outside + sum(exponentials.values())
        return ({item: value / denominator for item, value in exponentials.items()},
                outside / denominator)

    def log_likelihood(self, params):
        total = 0.0
        for observation in self.observations:
            probabilities, outside_prob = self.probabilities(params, observation)
            probability = outside_prob if observation['choice'] is None \
                else probabilities[observation['choice']]
            total += math.log(probability)
        return total

    def score_and_hessian(self, params):
        """解析得分（每条到店一行，用于 BHHH）与观测信息矩阵（-H）。"""
        dim = self.dim
        scores = [[0.0] * dim for _ in self.observations]
        information = [[0.0] * dim for _ in range(dim)]
        for row, observation in enumerate(self.observations):
            score = scores[row]
            probabilities, outside_prob = self.probabilities(params, observation)
            chosen = observation['choice']
            # 每个备选 -> (概率, 稀疏特征 {参数坐标: 特征值})，外部选项只含门店项。
            alternatives = []
            for item, probability in probabilities.items():
                features = {}
                if item in self.asc_index:
                    features[self.asc_index[item]] = 1.0
                features[self.index_beta] = math.log(
                    observation['choices'][item] / self.mean_price)
                alternatives.append((probability, features))
            outside_coordinate = self.index_outside[observation['store']]
            alternatives.append((outside_prob, {outside_coordinate: 1.0}))
            # 期望特征 x̄ = Σ_k P_k x_k。
            expected = defaultdict(float)
            for probability, features in alternatives:
                for coordinate, feature in features.items():
                    expected[coordinate] += probability * feature
            # 得分 = x(选中) - x̄。
            for coordinate, value in expected.items():
                score[coordinate] -= value
            if chosen is None:
                score[outside_coordinate] += 1.0
            else:
                if chosen in self.asc_index:
                    score[self.asc_index[chosen]] += 1.0
                score[self.index_beta] += math.log(
                    observation['choices'][chosen] / self.mean_price)
            # 信息矩阵 I_ab = Σ_k P_k (x_ka - x̄_a)(x_kb - x̄_b)。
            active = sorted(expected)
            for probability, features in alternatives:
                centered = {coordinate: features.get(coordinate, 0.0) - expected[coordinate]
                            for coordinate in active}
                for a in active:
                    if centered[a] == 0.0:
                        continue
                    for b in active:
                        information[a][b] += probability * centered[a] * centered[b]
        total = [sum(scores[row][i] for row in range(len(self.observations)))
                 for i in range(dim)]
        return total, scores, information

    def fit(self, max_iterations=40, tolerance=1e-9, ridge_beta=PRICE_RIDGE):
        """带价格系数岭先验的牛顿-Raphson 极大后验估计；配回退线搜索。"""
        params = self.initial()
        value = self.objective(params, ridge_beta)
        for _ in range(max_iterations):
            gradient, scores, information = self.score_and_hessian(params)
            # 岭先验：目标增加 -0.5*ridge*beta^2，梯度减 ridge*beta，信息加 ridge。
            gradient[self.index_beta] -= ridge_beta * params[self.index_beta]
            information[self.index_beta][self.index_beta] += ridge_beta
            if math.sqrt(sum(g * g for g in gradient)) < tolerance:
                break
            step = _solve_positive(information, gradient)  # delta = I^-1 g（最大化）
            if step is None:
                step = [0.1 * g for g in gradient]
            alpha = 1.0
            candidate = None
            for _ in range(50):
                candidate = [params[i] + alpha * step[i] for i in range(self.dim)]
                try:
                    candidate_value = self.objective(candidate, ridge_beta)
                except (ValueError, OverflowError):
                    candidate_value = float('-inf')
                if math.isfinite(candidate_value) and candidate_value >= value + 1e-11:
                    break
                alpha *= 0.5
            else:
                break
            params, value = candidate, candidate_value
        # 后验协方差用 BHHH 加岭项求逆；标准误据此计算。
        covariance = self._bhhs_covariance(scores, ridge_beta)
        return self._package(params, covariance, self.log_likelihood(params), information,
                             ridge_beta)

    def objective(self, params, ridge_beta):
        return self.log_likelihood(params) - 0.5 * ridge_beta * params[self.index_beta] ** 2

    def _bhhs_covariance(self, scores, ridge_beta=0.0):
        """BHHH：协方差 = (Σ s_n s_n' + ridge)^-1；奇异时收缩到对角矩阵。"""
        dim = self.dim
        information = [[0.0] * dim for _ in range(dim)]
        for score in scores:
            for i in range(dim):
                for j in range(dim):
                    information[i][j] += score[i] * score[j]
        information[self.index_beta][self.index_beta] += ridge_beta
        for attempt in range(6):
            covariance = _invert(information)
            if covariance is not None:
                return covariance
            for i in range(dim):
                information[i][i] += 10 ** (attempt - 4)
        return [[(1.0 / information[i][i] if information[i][i] > 1e-12 else 1.0)
                 if i == j else 0.0 for j in range(dim)] for i in range(dim)]

    def _package(self, params, covariance, log_likelihood, information, ridge_beta):
        asc = {item: (params[self.asc_index[item]] if item in self.asc_index else 0.0)
               for item in self.item_ids}
        standard_errors = {}
        names = self.parameter_names
        for index, name in enumerate(names):
            variance = covariance[index][index]
            standard_errors[name] = math.sqrt(variance) if variance > 0 else None
        return {
            'params': params,
            'covariance': covariance,
            'information': information,
            'asc': asc,
            'beta_price': params[self.index_beta],
            'outside': {store: params[self.index_outside[store]] for store in self.stores},
            'mean_price': self.mean_price,
            'log_likelihood': log_likelihood,
            'ridge_beta': ridge_beta,
            'n_observations': len(self.observations),
            'standard_errors': standard_errors,
            'parameter_names': names,
        }


def _cholesky(matrix):
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                value = matrix[i][i] - total
                if value <= 1e-12:
                    return None
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = (matrix[i][j] - total) / lower[j][j]
    return lower


def _solve_positive(matrix, rhs):
    """对正定矩阵解 A x = rhs（Cholesky）；奇异返回 None。"""
    lower = _cholesky(matrix)
    if lower is None:
        return None
    n = len(rhs)
    y = [0.0] * n
    for i in range(n):
        y[i] = (rhs[i] - sum(lower[i][j] * y[j] for j in range(i))) / lower[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(lower[j][i] * x[j] for j in range(i + 1, n))) / lower[i][i]
    return x


def _invert(matrix):
    """带部分主元的高斯-约当求逆；奇异返回 None。"""
    n = len(matrix)
    work = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [value / scale for value in work[column]]
        for row in range(n):
            if row == column:
                continue
            factor = work[row][column]
            if factor:
                work[row] = [value - factor * work[column][j] for j, value in enumerate(work[row])]
    return [row[n:] for row in work]


# --------------------------------------------------------------------------- #
# 拟合入口、参数抽样与新商品先验
# --------------------------------------------------------------------------- #

def fit_model(observations, items):
    if not observations:
        raise ChoiceDataError('当前范围没有到店记录，无法估计选择模型')
    model = MultinomialLogit(observations, items)
    fitted = model.fit()
    support = support_matrix(observations, items)
    fitted['model'] = model
    fitted['support'] = support
    fitted['null_log_likelihood'] = sum(
        math.log(1.0 / (len(observation['choices']) + 1)) for observation in observations)
    fitted['mc_fadden_r2'] = 1.0 - fitted['log_likelihood'] / fitted['null_log_likelihood']
    fitted['diagnostics'] = diagnostics(fitted, observations)
    return fitted


def diagnostics(fitted, observations):
    """模型可解释范围与识别诊断，供页面和接口如实呈现。"""
    model = fitted['model']
    predicted = {item: 0.0 for item in model.item_ids}
    predicted['none'] = 0.0
    observed = {item: 0 for item in model.item_ids}
    observed['none'] = 0
    for observation in observations:
        probabilities, outside_prob = model.probabilities(fitted['params'], observation)
        for item, probability in probabilities.items():
            predicted[item] += probability
        predicted['none'] += outside_prob
        observed[observation['choice'] or 'none'] += 1
    beta_se = fitted['standard_errors'].get('beta:log_price')
    weak_price = beta_se is not None and abs(fitted['beta_price']) < beta_se
    return {
        'predicted_shares': {key: predicted[key] / len(observations)
                             for key in model.item_ids + ['none']},
        'observed_shares': {key: observed[key] / len(observations)
                            for key in model.item_ids + ['none']},
        'price_coefficient': fitted['beta_price'],
        'price_coefficient_se': beta_se,
        'price_coefficient_sign': '负向' if fitted['beta_price'] < 0 else '非负向（需复核）',
        'price_effect_weak': weak_price,
        'ridge_beta': fitted.get('ridge_beta', 0.0),
        'note_nesting': '自由估计的巢参数与价格系数强混淆且似然在边界退化，资料不能拒绝 MNL，故不采用未经检验的嵌套替代结构。',
        'note_price': ('价格系数标准误大于点估计，价格效应识别较弱；模型对调价的结论应结合宽区间阅读，'
                       '不能宣称精确的价格弹性。') if weak_price else
                      '价格系数点估计为负且相对标准误可辨，调价方向结论可用，但仍以区间为准。',
    }


def sample_parameters(fitted, count=DRAWS, seed=SEED):
    """多元正态抽样；Cholesky 失败时退回独立正态（区间只会更宽）。"""
    params = fitted['params']
    covariance = fitted['covariance']
    dim = len(params)
    lower = _cholesky(covariance)
    rng = random.Random(seed)
    draws = []
    for _ in range(count):
        standard = [_normal(rng) for _ in range(dim)]
        if lower is None:
            draws.append([params[i] + math.sqrt(max(covariance[i][i], 0.0)) * standard[i]
                          for i in range(dim)])
        else:
            draws.append([params[i] + sum(lower[i][j] * standard[j] for j in range(i + 1))
                          for i in range(dim)])
    return draws


def _normal(rng):
    return math.sqrt(-2.0 * math.log(rng.random())) * math.cos(2.0 * math.pi * rng.random())


def new_item_prior(item_id, attributes, fitted, observations):
    """没有任何到店观测的新商品：属性层级先验 + 固定宽先验方差。

    ASC 取同 family、同 size 已估计商品的均值（不足时退同 family 均值，再退总体均值）；
    该值不是估计结果，调用方必须按 support=0、宽区间呈现。
    """
    family = attributes['family']
    size = attributes['size']
    model = fitted['model']
    if family not in model.families:
        raise ChoiceDataError(f'新商品 {item_id} 的 family {family} 没有历史商品可借鉴')
    asc = fitted['asc']
    observed = [item for item in model.item_ids
                if fitted['support']['available_visits'].get(item, 0) > 0]
    same_both = [item for item in observed
                 if model.items[item]['family'] == family and model.items[item]['size'] == size]
    same_family = [item for item in observed if model.items[item]['family'] == family]
    pool = same_both or same_family or observed
    prior_asc = sum(asc[item] for item in pool) / len(pool)
    prices = sorted(price for observation in observations
                    for item, price in observation['choices'].items()
                    if model.items[item]['family'] == family)
    reference_price = prices[len(prices) // 2] if prices else fitted['mean_price']
    return {
        'item': item_id, 'family': family, 'size': size,
        'prior_asc': prior_asc, 'prior_sd': NEW_ITEM_PRIOR_SD,
        'reference_price': reference_price,
        'borrowed_from': sorted(pool),
    }
