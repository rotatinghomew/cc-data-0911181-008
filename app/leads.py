"""关联线索分析。

只使用本地的班次、到场记录、教师、代办人和场地字段，给出需要一起查看的
线索并说明理由。分析不作违规认定，不涉及处罚，也不读取外部个人资料。

评分刻意不读取历史复核结论：历史样本只来自过去的挑选规则（示例数据中
全部为 large_amount），未复核班次不是负例。历史结论只用于事后比较新旧
排序价值，并且只在已复核子集内比较，避免把旧的挑选习惯学得更牢。
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
from math import floor, log

from .storage import DataError

# 单笔材料缺口本身只是待核对事项，不能等同于套领。
PER_CLAIM_WEIGHTS = {
    'missing_attachment': 0.9,
    'negative_amount': 1.0,
    'no_presence_records': 0.4,
}
PER_CLAIM_CAVEATS = {
    'missing_attachment': 'missing_attachment_is_a_document_gap_not_fraud',
    'negative_amount': 'negative_amount_needs_correction_review',
    'no_presence_records': 'missing_records_are_not_evidence_of_fraud',
}
MIN_ATTENDANCE_SHARED = 3
MIN_ATTENDANCE_OVERLAP = 0.8
MIN_PAIR_SCORE = 1.0
MAX_GROUP_SIZE = 8
DEFAULT_GROUP_SHARE = 0.4
DEFAULT_DAILY_BUDGET = 5
DOCUMENT_GAP_WEIGHT = 0.3
# 同组部分复核后，已复核关系降为背景权重；不删除线索，也不按结论好/坏改分。
CONTEXT_EDGE_FACTOR = 0.5

DISCLAIMER = '线索仅供人工复核，不构成违规认定，也不作出处罚决定。'
SIGNAL_NOTES = [
    '共用代办人或场地在本地数据中很常见，单独一条关系不构成线索。',
    '到场名单重合可能来自正常续班、同一批学员或同一场地排课，只作为需要一起查看的理由。',
    '缺失签到记录不等于套领；未复核的班次也不是负例。',
    '分析不读取外部个人资料，也不根据历史复核结果调整排序。',
]


def _specificity(seen, total):
    """取值越少见权重越高；公共关系的权重会自然降低。"""
    if total <= 1 or seen <= 0:
        return 1.0
    return round(max(0.0, min(1.0, log(total / seen) / log(total))), 3)


def _review_states(store, journal):
    """已复核、已回避和未复核是三种状态；回避不视为已查。"""
    states = {}
    for row in store.table('reviews'):
        states[row['claim']] = {'review_state': 'reviewed', 'review_outcome': row['outcome'],
                                'review_selection': row.get('selection')}
    if journal is not None:
        for row in journal.all('audit_assignment'):
            if row.get('status') == 'reviewed':
                current = states.setdefault(row['claim'], {'review_state': 'reviewed',
                                                           'review_outcome': None,
                                                           'review_selection': None})
                current['review_state'] = 'reviewed'
                if row.get('selection') is not None:
                    current['review_selection'] = row.get('selection')
            elif row.get('status') == 'recused':
                current = states.setdefault(row['claim'], {'review_state': 'recused',
                                                           'review_outcome': None,
                                                           'review_selection': None})
                if current['review_state'] != 'reviewed':
                    current['review_state'] = 'recused'
    for row in store.table('claims'):
        states.setdefault(row['claim'], {'review_state': 'unreviewed', 'review_outcome': None,
                                         'review_selection': None})
    return states


def _closed_claims(store, journal):
    """复核完成的班次不再进入计划；回避的班次仍待复核。"""
    closed = {row['claim'] for row in store.table('reviews')}
    if journal is not None:
        for row in journal.all('audit_assignment'):
            if row.get('status') == 'reviewed':
                closed.add(row['claim'])
    return closed


def _pair_signals(first, second, people, specifics, permissions):
    """计算一对班次的关联信号；两个独立关系族以上才可能成为线索。"""
    cross_school = first['school'] != second['school']
    families = {}
    reasons = []
    caveats = []

    if first['session'] == second['session'] and cross_school:
        families['session'] = round(1.2 * specifics['session'], 3)
        reasons.append('same_session_across_schools:' + str(first['session']))

    if first['teacher'] == second['teacher']:
        weight = 1.1 * specifics['teacher']
        if cross_school:
            weight *= 1.15
            reasons.append('same_teacher_across_schools:' + str(first['teacher']))
        else:
            reasons.append('same_teacher_same_school:' + str(first['teacher']))
        families['teacher'] = round(weight, 3)

    if first['agent'] == second['agent']:
        permitted = first['agent'] in permissions
        weight = 0.9 * specifics['agent']
        if permitted:
            weight *= 0.35
            reasons.append('shared_agent_with_recorded_permission:' + str(first['agent']))
            caveats.append('shared_agent_permission_recorded')
        else:
            reasons.append('shared_agent:' + str(first['agent']))
            caveats.append('shared_agent_can_be_normal')
        families['agent'] = round(weight, 3)

    if first['venue'] == second['venue']:
        weight = 0.7 * specifics['venue']
        if not cross_school:
            weight *= 0.8
        families['venue'] = round(weight, 3)
        reasons.append('same_venue:' + str(first['venue']))
        caveats.append('shared_venue_is_common')

    people_first = people[first['claim']]
    people_second = people[second['claim']]
    shared = len(people_first & people_second)
    smaller = min(len(people_first), len(people_second))
    overlap = shared / smaller if smaller else 0.0
    if shared >= MIN_ATTENDANCE_SHARED and overlap >= MIN_ATTENDANCE_OVERLAP:
        weight = 1.4 * overlap + (0.1 if shared >= 5 else 0.0)
        families['attendance'] = round(weight, 3)
        reasons.append('attendance_overlap:{}_shared_of_{}_smaller_roster'.format(shared, smaller))
        caveats.append('roster_overlap_may_reflect_normal_cohort_continuity')

    score = round(sum(families.values()), 3)
    strong = bool({'attendance', 'session', 'teacher'} & set(families))
    qualified = (cross_school and len(families) >= 2 and score >= MIN_PAIR_SCORE
                 and strong)
    return {
        'score': score,
        'families': sorted(families),
        'reasons': reasons,
        'caveats': sorted(set(caveats)),
        'qualified': qualified,
        'cross_school': cross_school,
        'shared_people': shared,
        'roster_overlap': round(overlap, 3),
    }


def _aggregate(edges):
    reasons = []
    caveats = set()
    families = set()
    teachers = set()
    agents = set()
    venues = set()
    sessions = set()
    related = set()
    max_overlap = 0.0
    max_shared = 0
    attendance_edges = 0
    for edge in edges:
        families.update(edge['families'])
        caveats.update(edge['caveats'])
        related.update([edge['a'], edge['b']])
        if 'teacher' in edge['families']:
            teachers.add(edge['teacher'])
        if 'agent' in edge['families']:
            agents.add(edge['agent'])
        if 'venue' in edge['families']:
            venues.add(edge['venue'])
        if 'session' in edge['families']:
            sessions.add(edge['session'])
        if 'attendance' in edge['families']:
            attendance_edges += 1
            max_overlap = max(max_overlap, edge['roster_overlap'])
            max_shared = max(max_shared, edge['shared_people'])
    if teachers:
        reasons.append('多个班次共用教师：' + '、'.join(sorted(teachers)))
    if agents:
        reasons.append('多个班次共用代办人：' + '、'.join(sorted(agents)))
    if venues:
        reasons.append('多个班次使用同一场地：' + '、'.join(sorted(venues)))
    if sessions:
        reasons.append('同一班次编号出现在不同学校：' + '、'.join(sorted(sessions)))
    if attendance_edges:
        reasons.append('{} 组班次的到场名单高度重合，最多共用 {} 人，最高重合度 {:.0f}%'.format(
            attendance_edges, max_shared, max_overlap * 100))
    return {
        'reasons': reasons,
        'caveats': sorted(caveats),
        'families': sorted(families),
        'related_claims': sorted(related),
        'max_shared_people': max_shared,
        'max_roster_overlap': round(max_overlap, 3),
    }


def _policy_limits(store):
    policy = store.find('policies', policy='current')
    share = policy.get('max_group_share', DEFAULT_GROUP_SHARE)
    try:
        share = float(share)
    except (TypeError, ValueError):
        share = DEFAULT_GROUP_SHARE
    if not 0 < share <= 1:
        share = DEFAULT_GROUP_SHARE
    size = policy.get('max_group_size', MAX_GROUP_SIZE)
    if not isinstance(size, int) or isinstance(size, bool) or not 2 <= size <= 100:
        size = MAX_GROUP_SIZE
    return share, size


def group_cap(policy, budget):
    """同一关联组每天可占的名额上限，至少 1 个。"""
    share = policy.get('max_group_share', DEFAULT_GROUP_SHARE)
    try:
        share = float(share)
    except (TypeError, ValueError):
        share = DEFAULT_GROUP_SHARE
    if not 0 < share <= 1:
        share = DEFAULT_GROUP_SHARE
    return max(1, min(budget, floor(budget * share)))


def _build_components(store, journal):
    """在全部班次上构建关联组件，组件不随复核进度改变编号或成员。

    已经复核的班次仍是关系背景，但不再作为待复核目标。这样部分完成只会
    调整当前风险和剩余成员，不会把未复核班次连带结案。
    """
    claim_rows = store.table('claims')
    claim_by_id = {row['claim']: row for row in claim_rows}
    identifiers = sorted(claim_by_id)
    total = len(identifiers)
    people = defaultdict(set)
    record_counts = Counter()
    for row in store.table('presence'):
        people[row['claim']].add(row['person'])
        record_counts[row['claim']] += 1
    permissions = {row['agent'] for row in store.table('permissions')}
    frequency = {field: Counter(row[field] for row in claim_rows)
                 for field in ('teacher', 'agent', 'venue', 'session')}
    states = _review_states(store, journal)
    closed = _closed_claims(store, journal)
    share, max_group_size = _policy_limits(store)

    def specificity(field, value):
        return _specificity(frequency[field][value], total)

    # 只比较至少共用一个关系取值的候选对，避免随数据量增长做全量两两比较。
    buckets = defaultdict(list)
    for claim in identifiers:
        row = claim_by_id[claim]
        for field in ('teacher', 'agent', 'venue', 'session'):
            buckets[(field, row[field])].append(claim)
    candidate_pairs = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for first, second in combinations(sorted(members), 2):
            candidate_pairs.add((first, second))

    candidates = []
    for first, second in sorted(candidate_pairs):
        first_row = claim_by_id[first]
        second_row = claim_by_id[second]
        if first_row['school'] == second_row['school']:
            continue  # 跨校是关联线索的必要条件，同校正常续班不单独作为线索。
        pair_specifics = {
            'teacher': specificity('teacher', first_row['teacher']),
            'agent': specificity('agent', first_row['agent']),
            'venue': specificity('venue', first_row['venue']),
            'session': specificity('session', first_row['session']),
        }
        signals = _pair_signals(first_row, second_row, people, pair_specifics, permissions)
        if not signals['qualified']:
            continue
        candidates.append((signals['score'], first, second, signals,
                           first_row['teacher'], first_row['agent'], first_row['venue'],
                           first_row['session']))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

    parent = {claim: claim for claim in identifiers}
    size = {claim: 1 for claim in identifiers}

    def find(claim):
        while parent[claim] != claim:
            parent[claim] = parent[parent[claim]]
            claim = parent[claim]
        return claim

    edges = []
    suppressed = []
    incident = defaultdict(list)
    for score, first, second, signals, teacher, agent, venue, session in candidates:
        left, right = find(first), find(second)
        if left == right:
            continue
        if size[left] + size[right] > max_group_size:
            suppressed.append({'claims': sorted([first, second]), 'score': score,
                               'reason': 'group_size_cap',
                               'note': '仍有共同关系，但为避免形成无法逐笔核查的大组，未合并'})
            continue
        parent[right] = left
        size[left] += size[right]
        edge = {'a': first, 'b': second, 'score': score, 'families': signals['families'],
                'reasons': signals['reasons'], 'caveats': signals['caveats'],
                'shared_people': signals['shared_people'],
                'roster_overlap': signals['roster_overlap'],
                'teacher': teacher, 'agent': agent, 'venue': venue, 'session': session}
        edges.append(edge)
        incident[first].append(edge)
        incident[second].append(edge)

    members = defaultdict(list)
    for claim in identifiers:
        members[find(claim)].append(claim)
    root_edges = defaultdict(list)
    for edge in edges:
        root_edges[find(edge['a'])].append(edge)
    group_ids = {}
    claim_root = {}
    for root, claims in members.items():
        claims.sort()
        group_ids[root] = 'G' + sha256(','.join(claims).encode()).hexdigest()[:8]
        for claim in claims:
            claim_root[claim] = root

    return {
        'claims': claim_rows,
        'claim_by_id': claim_by_id,
        'identifiers': identifiers,
        'people': people,
        'record_counts': record_counts,
        'states': states,
        'closed': closed,
        'edges': edges,
        'incident': incident,
        'members': {root: sorted(claims) for root, claims in members.items()},
        'root_edges': root_edges,
        'group_ids': group_ids,
        'claim_root': claim_root,
        'suppressed': suppressed,
        'share': share,
        'max_group_size': max_group_size,
    }


def model(store, journal=None, scope='all'):
    """构建线索模型。scope=open 时已复核班次不再进入排序和计划。"""
    if scope not in ('all', 'open'):
        raise DataError('invalid scope: ' + str(scope))
    built = _build_components(store, journal)
    closed = built['closed']
    active = set(built['identifiers']) if scope == 'all' else {
        claim for claim in built['identifiers'] if claim not in closed}
    claim_by_id = built['claim_by_id']
    people = built['people']
    record_counts = built['record_counts']
    states = built['states']

    claims_view = {}
    for claim in sorted(active):
        row = claim_by_id[claim]
        base_reasons = []
        base_caveats = []
        document_score = 0.0
        document_issues = []
        if not row['attachments']:
            document_issues.append('missing_attachment')
        if row['amount'] < 0:
            document_issues.append('negative_amount')
        if record_counts[claim] == 0:
            document_issues.append('no_presence_records')
        for issue in document_issues:
            document_score += PER_CLAIM_WEIGHTS.get(issue, 0.0)
            base_reasons.append('单笔检查项：' + issue)
            caveat = PER_CLAIM_CAVEATS.get(issue)
            if caveat:
                base_caveats.append(caveat)

        current_edges = [edge for edge in built['incident'][claim]
                         if edge['a'] in active and edge['b'] in active]
        context_edges = [edge for edge in built['incident'][claim]
                         if (edge['a'] in closed) != (edge['b'] in closed)]
        context_claims = set()
        for edge in context_edges:
            if edge['a'] in closed and edge['a'] != claim:
                context_claims.add(edge['a'])
            if edge['b'] in closed and edge['b'] != claim:
                context_claims.add(edge['b'])
        related = _aggregate(current_edges)
        edge_max = max((edge['score'] for edge in current_edges), default=0.0)
        context_max = max((edge['score'] for edge in context_edges), default=0.0)
        if claim in closed:
            context_max = 0.0
        # 关联依据随复核进度调整：当前关联优先，已复核关联只保留背景权重。
        relation_score = max(edge_max, CONTEXT_EDGE_FACTOR * context_max)
        reasons = base_reasons + related['reasons']
        if claim in closed:
            context_claims = set()
        elif scope == 'all':
            if context_claims:
                reasons.append('相关班次 ' + '、'.join(sorted(context_claims))
                               + ' 已复核；这里只列出原始关系，未据此调整结论。')
        elif context_claims and not current_edges:
            reasons.append('关联依据已调整：' + '、'.join(sorted(context_claims))
                           + ' 已复核，原关联降为背景权重，也未连带结案其余班次。')
        elif context_claims:
            reasons.append('另有已复核的相关班次 ' + '、'.join(sorted(context_claims))
                           + '：其关联只按背景权重保留，其余班次仍需单独复核。')

        root = built['claim_root'].get(claim)
        group_id = built['group_ids'].get(root)
        group_size = len(built['members'].get(root, []))
        state = states.get(claim, {'review_state': 'unreviewed', 'review_outcome': None,
                                   'review_selection': None})
        claims_view[claim] = {
            'claim': claim,
            'school': row['school'],
            'session': row['session'],
            'teacher': row['teacher'],
            'agent': row['agent'],
            'venue': row['venue'],
            'amount': row['amount'],
            'attachments': row['attachments'],
            'document_issues': document_issues,
            'presence_records': record_counts[claim],
            'unique_rollcall_people': len(people[claim]),
            # 关联线索优先；单笔材料缺口只作次要加分，避免回到逐笔金额/附件排序。
            'risk': round(relation_score + DOCUMENT_GAP_WEIGHT * document_score, 3),
            'relation_score': round(edge_max, 3),
            'context_relation_score': round(CONTEXT_EDGE_FACTOR * context_max, 3),
            'document_score': round(document_score, 3),
            'group_id': group_id if group_size >= 2 else None,
            'reasons': reasons,
            'caveats': sorted(set(base_caveats + related['caveats']
                                  + (['grouping_is_a_lead_not_a_finding']
                                     if current_edges else []))),
            'related_claims': [other for other in related['related_claims'] if other != claim],
            'context_reviewed_claims': sorted(context_claims),
            'max_shared_people': related['max_shared_people'],
            'max_roster_overlap': related['max_roster_overlap'],
            'review_state': state['review_state'],
            'review_outcome': state['review_outcome'],
            'review_selection': state['review_selection'],
        }

    groups = []
    for root, members in built['members'].items():
        if len(members) < 2:
            continue
        # 待复核/已复核按复核状态定义，不随 scope 改变；scope 只决定哪些成员参与排序。
        open_members = [claim for claim in members if claim not in closed]
        reviewed_members = [claim for claim in members if claim in closed]
        if scope == 'open' and not open_members:
            continue
        group_id = built['group_ids'][root]
        current_edges = [edge for edge in built['root_edges'][root]
                         if edge['a'] in active and edge['b'] in active]
        # 已复核成员仍解释「为什么这些班次曾被放在一起」，但不再计入当前风险。
        all_edges = built['root_edges'][root] if scope == 'all' else current_edges
        aggregate = _aggregate(all_edges)
        caveats = set(aggregate['caveats'])
        caveats.add('grouping_is_a_lead_not_a_finding')
        if reviewed_members and open_members:
            caveats.add('review_progress_does_not_close_remaining_claims')
        reasons = list(aggregate['reasons'])
        if reviewed_members and scope == 'all':
            reasons.append('组内 ' + '、'.join(sorted(reviewed_members))
                           + ' 已复核；这里只列出原始关系，未据此调整结论。')
        elif reviewed_members:
            reasons.append('关联依据已调整：' + '、'.join(sorted(reviewed_members))
                           + ' 已复核，其关联降为背景权重，也未连带结案其余班次。')
        risks = [claims_view[claim]['risk'] for claim in members if claim in claims_view]
        groups.append({
            'group_id': group_id,
            'claims': members,
            'open_claims': sorted(open_members),
            'reviewed_claims': sorted(reviewed_members),
            'schools': sorted({claim_by_id[claim]['school'] for claim in members}),
            'cross_school': len({claim_by_id[claim]['school'] for claim in members}) > 1,
            'relation_families': aggregate['families'],
            'reasons': reasons,
            'caveats': sorted(caveats),
            'risk': round(max(risks), 3) if risks else 0.0,
            'max_shared_people': aggregate['max_shared_people'],
            'max_roster_overlap': aggregate['max_roster_overlap'],
            'partially_reviewed': bool(reviewed_members) and bool(open_members),
        })
    # 组风险取当前成员最高风险，高风险成员不会因为合并成大组而消失。
    groups.sort(key=lambda group: (-group['risk'], group['claims'][0]))
    for position, group in enumerate(groups, 1):
        group['rank'] = position
        for claim in group['claims']:
            if claim in claims_view:
                claims_view[claim]['group_rank'] = position
    # 同风险时按组名次和班次编号排序，不以金额破平，避免旧金额排序借平局回归。
    ranked = sorted(claims_view.values(),
                    key=lambda row: (-row['risk'], row.get('group_rank') or 10 ** 6,
                                     row['claim']))
    for position, row in enumerate(ranked, 1):
        row['rank'] = position
    return {
        'generated_from': {
            'claims': len(built['claims']),
            'presence': sum(built['record_counts'].values()),
            'scope': scope,
            'open_claims': len(active),
            'reviewed_claims': len(closed),
        },
        'policy': {'max_group_share': built['share'],
                   'max_group_size': built['max_group_size']},
        'groups': groups,
        'claims': ranked,
        'ranked_claims': [row['claim'] for row in ranked],
        'suppressed_links': built['suppressed'],
        'signal_notes': SIGNAL_NOTES,
        'disclaimer': DISCLAIMER,
    }


def group_for(store, claim_id, journal=None, scope='open'):
    """返回某班次所在的线索组；没有组时返回 None。"""
    result = model(store, journal, scope)
    for group in result['groups']:
        if claim_id in group['claims']:
            return group
    return None


def plan(store, query, journal=None):
    """生成每日复核清单：组内限流，同时公开被延后的高风险班次。"""
    from .storage import integer

    policy = store.find('policies', policy='current')
    budget = integer(query, 'budget', policy.get('daily_budget', DEFAULT_DAILY_BUDGET), 1, 100)
    cap = group_cap(policy, budget)
    analysis = model(store, journal, 'open')
    assigned = set()
    group_counts = Counter()
    if journal is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        for row in journal.all('audit_assignment'):
            if row.get('status') == 'reviewed':
                continue
            # 任何日期已派工的班次都不重复进入清单；组名额只按当日占用计算。
            if row.get('status') == 'assigned':
                assigned.add(row['claim'])
                if row.get('group_id') and row.get('assigned_on') == today():
                    group_counts[row['group_id']] += 1
    ordered = sorted(analysis['claims'],
                     key=lambda row: (-row['risk'], row.get('group_rank') or 10 ** 6,
                                      row['claim']))
    selected = []
    deferred = []
    for row in ordered:
        entry = {'claim': row['claim'], 'group_id': row['group_id'], 'risk': row['risk'],
                 'rank': row['rank']}
        if row['claim'] in assigned:
            deferred.append(dict(entry, reason='already_assigned'))
            continue
        if len(selected) >= budget:
            deferred.append(dict(entry, reason='daily_budget'))
            continue
        group = row['group_id']
        if group is not None and group_counts[group] >= cap:
            deferred.append(dict(entry, reason='group_daily_cap'))
            continue
        selected.append({
            'claim': row['claim'],
            'school': row['school'],
            'amount': row['amount'],
            'risk': row['risk'],
            'rank': row['rank'],
            'group_id': group,
            'document_issues': row['document_issues'],
            'reasons': row['reasons'],
            'caveats': row['caveats'],
            'review_state': row['review_state'],
        })
        if group is not None:
            group_counts[group] += 1
    deferred.sort(key=lambda row: (-row['risk'], row['claim']))
    return {
        'policy': 'lead_groups',
        'budget': budget,
        'group_cap': cap,
        'group_share': policy.get('max_group_share', DEFAULT_GROUP_SHARE),
        'selected': selected,
        'deferred': deferred,
        'groups_represented': dict(sorted(group_counts.items())),
        'notes': [
            '同一关联组每天最多占 ' + str(cap) + ' 个名额，避免挤占其他线索。',
            '被组内限流或日名额延后的班次列在 deferred 中，不会静默消失。',
            '已复核班次不进入清单；回避的班次仍待复核。',
            '分组只是需要一起查看的理由，不是违规证据。',
            DISCLAIMER,
        ],
    }

