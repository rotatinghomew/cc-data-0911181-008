"""复核派工。

派工不只截取排行榜前几条：每笔都检查学校及教师、代办人、场地的利益关联，
并检查审核员当日剩余名额和同一关联组的每日名额上限。找不到合适审核员的
班次会明确列出原因，而不是静默略过。
"""

from collections import Counter
from datetime import datetime, timezone

from . import leads
from .storage import DataError

CONFLICT_FIELDS = ('school', 'teacher', 'agent', 'venue', 'session')
DEFAULT_BUDGET = 5


def today():
    return datetime.now(timezone.utc).date().isoformat()


def _reviewer_conflicts(reviewer):
    """兼容现有 conflicts（学校列表），并支持按字段列出的利益关联。"""
    grouped = {field: set() for field in CONFLICT_FIELDS}
    schools = reviewer.get('conflicts') or []
    if not isinstance(schools, list) or not all(isinstance(value, str) for value in schools):
        raise DataError('invalid reviewer conflicts')
    for school in schools:
        grouped['school'].add(school)
    explicit = reviewer.get('conflicts_by_field') or {}
    if not isinstance(explicit, dict):
        raise DataError('invalid reviewer conflicts')
    for field, values in explicit.items():
        if field not in grouped:
            raise DataError('unknown conflict field: ' + str(field))
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise DataError('conflict values must be a list of strings: ' + field)
        grouped[field].update(values)
    return grouped


def _conflict_detail(claim_row, reviewer):
    conflicts = _reviewer_conflicts(reviewer)
    hits = []
    for field in CONFLICT_FIELDS:
        value = claim_row.get(field)
        if value is not None and value in conflicts[field]:
            hits.append({'field': field, 'value': value})
    return hits


def _group_context(analysis, claim):
    for group in analysis['groups']:
        if claim in group['claims']:
            return group
    return None


def _open_claims(group):
    return set(group['open_claims']) if group else set()


def _group_conflicts(group, store, reviewer):
    """派到组内任一班次都视为要查看组内关系，因此需避开所有开放成员。"""
    details = []
    for member in sorted(_open_claims(group)):
        hits = _conflict_detail(store.find('claims', claim=member), reviewer)
        if hits:
            details.append({'claim': member, 'matches': hits})
    return details


def _today_counts(journal):
    """当日已占用的核查名额；复核完成仍已占用，回避则不占用。"""
    counts = Counter()
    group_counts = Counter()
    for row in journal.all('audit_assignment'):
        if row.get('assigned_on') != today():
            continue
        if row.get('status') not in ('assigned', 'reviewed'):
            continue
        counts[row['reviewer']] += 1
        if row.get('group_id'):
            group_counts[row['group_id']] += 1
    return counts, group_counts


def _exists(journal, kind, key):
    try:
        journal.get(kind, key)
    except DataError:
        return False
    return True


def _active_assignment(existing, claim):
    for row in existing:
        if row['claim'] == claim and row.get('status') in ('assigned', 'reviewed'):
            return row
    return None


def _record_assignment(store, journal, claim, reviewer, group, cap=None):
    claim_row = store.find('claims', claim=claim)
    counts, group_counts = _today_counts(journal)
    if counts[reviewer['reviewer']] >= reviewer.get('capacity', 0):
        raise DataError('reviewer capacity exceeded')
    if group:
        if cap is None:
            policy = store.find('policies', policy='current')
            cap = leads.group_cap(policy, policy.get('daily_budget', DEFAULT_BUDGET))
        if group_counts[group['group_id']] >= cap:
            raise DataError('group daily cap reached')
        open_members = sorted(_open_claims(group))
        conflicts = _group_conflicts(group, store, reviewer)
        if conflicts:
            raise DataError('reviewer conflict of interest: '
                            + ','.join(item['claim'] for item in conflicts))
        relation_basis = group['reasons']
        selection = 'lead_group'
    else:
        hits = _conflict_detail(claim_row, reviewer)
        if hits:
            raise DataError('reviewer conflict of interest: '
                            + ','.join(hit['field'] for hit in hits))
        open_members = [claim]
        relation_basis = []
        selection = 'manual'
    row = dict(claim=claim, school=claim_row['school'],
               reviewer=reviewer['reviewer'], status='assigned',
               assigned_on=today(), group_id=group['group_id'] if group else None,
               group_claims=open_members, selection=selection,
               relation_basis=relation_basis)
    return journal.put('audit_assignment', claim, row)


def assign(store, journal, payload):
    claim = store.find('claims', claim=payload['claim'])
    reviewer = store.find('reviewers', reviewer=payload['reviewer'])
    if not isinstance(reviewer.get('capacity'), int) or reviewer.get('capacity', 0) < 1:
        raise DataError('reviewer capacity must be a positive integer')
    analysis = leads.model(store, journal, 'all')
    group = _group_context(analysis, claim['claim'])
    with journal.transaction():
        existing = _active_assignment(journal.all('audit_assignment'), claim['claim'])
        if existing is not None:
            return existing
        return _record_assignment(store, journal, claim['claim'], reviewer, group)


def dispatch(store, journal, payload):
    """按线索计划批量派工，跳过有利益关联或名额不足的候选，不截断榜单。"""
    from .storage import integer

    policy = store.find('policies', policy='current')
    budget = integer({'budget': [str(payload.get('budget', policy.get('daily_budget',
                                                                     DEFAULT_BUDGET)))]},
                     'budget', policy.get('daily_budget', DEFAULT_BUDGET), 1, 100)
    requested = payload.get('reviewers')
    if requested is None:
        reviewers = store.table('reviewers')
    else:
        if not isinstance(requested, list) or not requested:
            raise DataError('reviewers must be a non-empty list')
        reviewers = []
        for identifier in requested:
            if not isinstance(identifier, str):
                raise DataError('reviewer identifiers must be strings')
            reviewers.append(store.find('reviewers', reviewer=identifier))
    analysis = leads.model(store, journal, 'open')
    cap = leads.group_cap(policy, budget)
    assigned = []
    skipped = []
    with journal.transaction():
        counts, group_counts = _today_counts(journal)
        active_claims = {row['claim'] for row in journal.all('audit_assignment')
                         if row.get('status') in ('assigned', 'reviewed')}
        for row in analysis['claims']:
            if len(assigned) >= budget:
                break
            claim = row['claim']
            if claim in active_claims:
                skipped.append({'claim': claim, 'reason': 'already_assigned',
                                'risk': row['risk']})
                continue
            group = _group_context(analysis, claim)
            group_id = group['group_id'] if group else None
            if group_id and group_counts[group_id] >= cap:
                skipped.append({'claim': claim, 'reason': 'group_daily_cap',
                                'group_id': group_id, 'risk': row['risk']})
                continue
            eligible = []
            conflict_notes = []
            for reviewer in reviewers:
                if counts[reviewer['reviewer']] >= reviewer.get('capacity', 0):
                    continue
                conflicts = (_group_conflicts(group, store, reviewer)
                             if group else _conflict_detail(
                                 store.find('claims', claim=claim), reviewer))
                if conflicts:
                    conflict_notes.append({'reviewer': reviewer['reviewer'],
                                           'reason': 'conflict_of_interest'})
                    continue
                remaining = reviewer.get('capacity', 0) - counts[reviewer['reviewer']]
                eligible.append((remaining, reviewer['reviewer'], reviewer))
            if not eligible:
                skipped.append({'claim': claim, 'reason': 'no_eligible_reviewer',
                                'group_id': group_id, 'risk': row['risk'],
                                'reviewers': conflict_notes})
                continue
            eligible.sort(key=lambda item: (-item[0], item[1]))
            reviewer = eligible[0][2]
            result = _record_assignment(store, journal, claim, reviewer, group, cap)
            counts[reviewer['reviewer']] += 1
            if group_id:
                group_counts[group_id] += 1
            assigned.append(result)
            active_claims.add(claim)
    return {
        'policy': 'lead_groups',
        'budget': budget,
        'group_cap': cap,
        'assigned': assigned,
        'skipped': skipped,
        'assigned_count': len(assigned),
        'notes': [
            '按线索风险顺序尝试派工，遇到利益关联、名额不足或组内限流时继续看后续班次。',
            '同一关联组当日最多 ' + str(cap) + ' 笔，避免挤占其他线索。',
            '被跳过的候选及原因全部保留，不会只交出一段截断的排行榜。',
            '派工只安排人工复核，不作出违规或处罚结论。',
        ],
    }


def recuse(store, journal, payload):
    """审核员回避：释放当日名额，班次保持待复核，可另行派工。"""
    with journal.transaction():
        assignment = journal.get('audit_assignment', payload['claim'])
        if assignment.get('reviewer') != payload.get('reviewer'):
            raise DataError('wrong reviewer')
        if assignment.get('status') == 'reviewed':
            raise DataError('claim already reviewed')
        if assignment.get('status') == 'recused':
            return assignment
        reason = payload.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise DataError('recusal reason required')
        # 同一审核员同日可能先回避、重新派工后再次回避，按序号保留每次记录。
        base_key = ':'.join([assignment['claim'], assignment['reviewer'], today()])
        recusal_key = base_key
        offset = 1
        while _exists(journal, 'audit_recusal', recusal_key):
            offset += 1
            recusal_key = base_key + ':' + str(offset)
        record = {
            'claim': assignment['claim'],
            'reviewer': assignment['reviewer'],
            'reason': reason.strip(),
            'recused_at': today(),
            'group_id': assignment.get('group_id'),
            'group_claims': assignment.get('group_claims', [assignment['claim']]),
            'requested_records': [],
        }
        journal.put('audit_recusal', recusal_key, record)
        assignment['status'] = 'recused'
        assignment['recused_at'] = today()
        journal.put('audit_assignment', assignment['claim'], assignment)
    replacement = payload.get('replacement')
    if replacement:
        return assign(store, journal, {'claim': assignment['claim'], 'reviewer': replacement})
    return assignment
