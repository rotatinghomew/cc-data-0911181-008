"""新旧排序价值比较。

历史复核只覆盖审核员真正挑中过的样本，未复核班次在评估中按未知处理，
不当作负例，也不进入分母。默认只在历史已复核子集内比较新旧排序，避免把
旧的挑选习惯当成全部班次的真实分布。
"""

from collections import Counter

from . import leads
from .storage import DataError, integer

KNOWN_OUTCOMES = ('supported', 'needs_correction', 'inconclusive')
CORRECTION = 'needs_correction'
SUPPORTED = 'supported'

EVALUATION_NOTES = [
    '未复核班次按未知处理，不当作负例；分母只包含已明确结论的班次。',
    '历史复核全部来自过去按金额挑中的样本，默认比较只在该已复核子集内进行，不能直接外推到全部班次。',
    '新排序不读取复核结论；评估只用于比较排序价值，不参与评分，也不会把过去的挑选习惯写回权重。',
    '未记录抽选概率时无法做总体估计；如后续复核记录 selection 与 selection_probability，可按概率加权并分层比较。',
]


def _labels(store, journal=None):
    labels = {}
    for row in store.table('reviews'):
        if row.get('outcome') not in KNOWN_OUTCOMES:
            continue
        labels[row['claim']] = {
            'outcome': row['outcome'],
            'selection': row.get('selection'),
            'selection_probability': row.get('selection_probability'),
            'source': 'historical_review',
            'reviewed_at': row.get('reviewed_at'),
        }
    if journal is not None:
        for row in journal.all('audit_result'):
            if row.get('outcome') not in KNOWN_OUTCOMES:
                continue
            labels[row['claim']] = {
                'outcome': row['outcome'],
                'selection': row.get('selection'),
                'selection_probability': row.get('selection_probability'),
                'source': 'audit_result',
                'reviewed_at': row.get('reviewed_at'),
            }
    return labels


def _amount_order(store):
    return [row['claim'] for row in sorted(store.table('claims'),
                                           key=lambda row: (-row['amount'], row['claim']))]


def _lead_order(store, journal=None):
    return [row['claim'] for row in leads.model(store, journal, 'all')['claims']]


def _rank_map(order):
    return {claim: position for position, claim in enumerate(order, 1)}


def _rate(counts):
    denominator = counts.get(CORRECTION, 0) + counts.get(SUPPORTED, 0)
    if not denominator:
        return None
    return round(counts.get(CORRECTION, 0) / denominator, 4)


def _metrics(order, labels, limit):
    top = order[:limit]
    known = [claim for claim in top if claim in labels]
    counts = Counter(labels[claim]['outcome'] for claim in known)
    return {
        'limit': limit,
        'claims': top,
        'reviewed_in_selection': len(known),
        'unreviewed_in_selection': len(top) - len(known),
        'outcomes': {outcome: counts.get(outcome, 0) for outcome in KNOWN_OUTCOMES},
        'decided': counts.get(CORRECTION, 0) + counts.get(SUPPORTED, 0),
        'needs_correction_rate': _rate(counts),
        'note': '未复核班次保留为未知，不计入 supporting/negative。',
    }


def _reviewed_metrics(order, labels, limit):
    reviewed_order = [claim for claim in order if claim in labels]
    top = reviewed_order[:limit]
    counts = Counter(labels[claim]['outcome'] for claim in top)
    return {
        'limit': limit,
        'reviewed_population': len(reviewed_order),
        'claims': top,
        'outcomes': {outcome: counts.get(outcome, 0) for outcome in KNOWN_OUTCOMES},
        'decided': counts.get(CORRECTION, 0) + counts.get(SUPPORTED, 0),
        'needs_correction_rate': _rate(counts),
        'note': '只在已复核子集内重排，避免未复核班次被当成负例或被金额排序挤出分母。',
    }


def _weighted_rate(claims, labels):
    numerator = 0.0
    denominator = 0.0
    for claim in claims:
        label = labels[claim]
        probability = label.get('selection_probability')
        outcome = label['outcome']
        if outcome not in (CORRECTION, SUPPORTED):
            continue
        if not isinstance(probability, (int, float)) or isinstance(probability, bool):
            return None
        if not 0 < probability <= 1:
            return None
        weight = 1.0 / probability
        denominator += weight
        if outcome == CORRECTION:
            numerator += weight
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _selection_bias(labels, total_claims):
    selection = Counter(label.get('selection') or 'unknown' for label in labels.values())
    probabilities = [label.get('selection_probability') for label in labels.values()]
    recorded = sum(1 for probability in probabilities if probability is not None)
    valid = bool(labels) and recorded == len(labels) and all(
        isinstance(probability, (int, float)) and not isinstance(probability, bool)
        and 0 < probability <= 1 for probability in probabilities)
    return {
        'total_claims': total_claims,
        'reviewed_claims': len(labels),
        'review_coverage': round(len(labels) / total_claims, 4) if total_claims else None,
        'selection_labels': dict(sorted(selection.items())),
        'selection_probability_recorded': recorded,
        'population_estimate_available': valid,
        'method': ('inverse_probability_weighted_reviewed_stratum'
                   if valid else 'conditional_on_historical_reviewed_sample'),
        'unreviewed_treatment': 'unknown_not_negative',
        'notes': EVALUATION_NOTES,
    }


def compare(store, query, journal=None):
    policy = store.find('policies', policy='current')
    limit = integer(query, 'budget', policy.get('daily_budget', 5), 1, 100)
    all_labels = _labels(store, journal)
    historical = {claim: label for claim, label in all_labels.items()
                  if label['source'] == 'historical_review'}
    old_order = _amount_order(store)
    new_order = _lead_order(store, journal)
    old_rank = _rank_map(old_order)
    new_rank = _rank_map(new_order)
    old_reviewed_order = [claim for claim in old_order if claim in historical]
    new_reviewed_order = [claim for claim in new_order if claim in historical]
    old_reviewed_rank = _rank_map(old_reviewed_order)
    new_reviewed_rank = _rank_map(new_reviewed_order)

    feedback = []
    for claim, label in sorted(historical.items(),
                               key=lambda item: (new_reviewed_rank.get(item[0], 10 ** 6),
                                                 item[0])):
        feedback.append({
            'claim': claim,
            'outcome': label['outcome'],
            'selection': label.get('selection'),
            'selection_probability': label.get('selection_probability'),
            'source': label['source'],
            'old_rank': old_rank.get(claim),
            'new_rank': new_rank.get(claim),
            'old_rank_within_reviewed': old_reviewed_rank.get(claim),
            'new_rank_within_reviewed': new_reviewed_rank.get(claim),
            'rank_change_within_reviewed': (old_reviewed_rank.get(claim, 0)
                                            - new_reviewed_rank.get(claim, 0)),
            'used_in_default_comparison': True,
        })
    recent = []
    for claim, label in sorted(all_labels.items()):
        if label['source'] == 'audit_result':
            recent.append({
                'claim': claim,
                'outcome': label['outcome'],
                'selection': label.get('selection'),
                'selection_probability': label.get('selection_probability'),
                'source': label['source'],
                'old_rank': old_rank.get(claim),
                'new_rank': new_rank.get(claim),
                'used_in_default_comparison': False,
            })

    corrections = [claim for claim, label in historical.items() if label['outcome'] == CORRECTION]
    lifts = [old_reviewed_rank[claim] - new_reviewed_rank[claim]
             for claim in corrections if claim in old_reviewed_rank and claim in new_reviewed_rank]
    old_reviewed_metrics = _reviewed_metrics(old_order, historical, limit)
    new_reviewed_metrics = _reviewed_metrics(new_order, historical, limit)
    bias = _selection_bias(historical, len(store.table('claims')))
    if bias['population_estimate_available']:
        old_reviewed_metrics['weighted_needs_correction_rate'] = _weighted_rate(
            old_reviewed_metrics['claims'], historical)
        new_reviewed_metrics['weighted_needs_correction_rate'] = _weighted_rate(
            new_reviewed_metrics['claims'], historical)
    else:
        old_reviewed_metrics['weighted_needs_correction_rate'] = None
        new_reviewed_metrics['weighted_needs_correction_rate'] = None

    return {
        'policy': {
            'old': 'descending_amount',
            'new': 'lead_groups_risk',
            'limit': limit,
        },
        'selection_bias': bias,
        'overall_selection': {
            'old': _metrics(old_order, historical, limit),
            'new': _metrics(new_order, historical, limit),
        },
        'reviewed_stratum': {
            'old': old_reviewed_metrics,
            'new': new_reviewed_metrics,
            'corrections': sorted(corrections),
            'correction_count': len(corrections),
            'mean_correction_rank_change': (round(sum(lifts) / len(lifts), 3)
                                            if lifts else None),
            'note': ('排名变化为旧名次减新名次，正数表示新排序在已复核子集中更早看到'
                     '需要更正的班次；样本很小，只作方向性参考。'),
        },
        'feedback_source': feedback,
        'recent_feedback': recent,
        'notes': EVALUATION_NOTES,
        'disclaimer': '评估只比较排序价值，不据此处罚，也不把复核结论用于自动评分。',
    }
