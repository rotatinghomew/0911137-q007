from collections import Counter, defaultdict
import math
from statistics import mean, median


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限实数')
    return value


def selected(value, group):
    return not group or value == group


def latest(rows, key, revision, visible, cutoff):
    result = {}
    for row in rows:
        if row[visible] > cutoff:
            continue
        previous = result.get(row[key])
        if previous is None or row[revision] > previous[revision]:
            result[row[key]] = row
    return list(result.values())


def output(rows, summary, notes):
    return {'rows': rows, 'summary': summary, 'notes': notes}


def run(data, options):
    cutoff, group = options['cutoff'], options['group']
    visits = {row['visit']: row for row in data['visits'] if row['day'] <= cutoff and selected(row['store'], group)}
    offers = defaultdict(dict)
    for offer in data['offers']:
        if offer['visit'] in visits and offer['shown_at'] <= visits[offer['visit']]['day']:
            offers[offer['visit']][offer['item']] = offer
    rows = []
    for key, visit in visits.items():
        available = {item: offer for item, offer in offers[key].items() if offer['available']}
        choice = visit['choice']
        valid = choice is None or choice in available
        price = number(available[choice]['price']) if choice is not None and valid else None
        rows.append(dict(visit=key, customer=visit['customer'], store=visit['store'], day=visit['day'], choice=choice, available=sorted(available), valid=valid, amount=price, outside=choice is None))
    return output(rows, {'到店次数': len(rows), '未购买': sum(row['outside'] for row in rows), '已购金额': round(sum(row['amount'] or 0 for row in rows), 2), '记录矛盾': sum(not row['valid'] for row in rows)}, ['售出占比不是偏好概率；尚未估计替代。'])
