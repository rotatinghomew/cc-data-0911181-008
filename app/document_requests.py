"""向申报学校索取复核材料。

多校关联组按学校分别索件：每份请求只包含本校签到记录和必要的关联理由，
其他学校只给出班次与签到数量等汇总，不附带个人签到明细。
"""

from datetime import datetime, timezone

from . import leads
from .storage import DataError


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _assignment(journal, claim):
    try:
        return journal.get('audit_assignment', claim)
    except DataError:
        raise DataError('unknown assignment: ' + str(claim)) from None


def _group(store, journal, claim):
    analysis = leads.model(store, journal, 'all')
    for group in analysis['groups']:
        if claim in group['claims']:
            return group
    return None


def _existing(journal, key):
    for row in journal.all('document_request'):
        if row.get('request_key') == key:
            return row
    return None


def _request_for_school(store, journal, assignment, group, school):
    # 只为仍需复核的班次索件；已复核学校不再被要求重复提供材料。
    if group:
        group_claims = sorted(set(group['open_claims']) | {assignment['claim']})
    else:
        group_claims = [assignment['claim']]
    if school not in {store.find('claims', claim=claim)['school'] for claim in group_claims}:
        raise DataError('school not related to this lead: ' + str(school))
    own_claims = sorted(claim for claim in group_claims
                        if store.find('claims', claim=claim)['school'] == school)
    key = (group['group_id'] + ':' + school) if group else assignment['claim']
    existing = _existing(journal, key)
    if existing is not None:
        _remember_request(journal, assignment, school, key)
        return existing

    records = []
    for claim in own_claims:
        records.extend(record['record'] for record in store.select('presence', claim=claim))
    other_schools = []
    if group:
        for other in sorted({store.find('claims', claim=claim)['school']
                             for claim in group_claims} - {school}):
            claims = [claim for claim in group_claims
                      if store.find('claims', claim=claim)['school'] == other]
            other_schools.append({
                'school': other,
                'claim_count': len(claims),
                'presence_record_count': sum(len(store.select('presence', claim=claim))
                                             for claim in claims),
            })
    row = {
        'request_key': key,
        'claim': assignment['claim'],
        'claims': own_claims,
        'group_id': group['group_id'] if group else None,
        'school': school,
        'requested_records': sorted(records),
        'record_count': len(records),
        'other_schools': other_schools,
        'relation_families': group['relation_families'] if group else [],
        'relation_basis': group['reasons'] if group else [],
        'status': 'requested',
        'requested_at': _today(),
        'privacy_note': ('只向本校提供本校签到记录；其他学校只提供数量汇总，'
                         '不包含个人签到明细。'),
    }
    journal.put('document_request', key, row)
    _remember_request(journal, assignment, school, key)
    return row


def _remember_request(journal, assignment, school, key):
    """记录每校请求；审核员本人的索件以派工学校为准，不被其他学校覆盖。"""
    keys = dict(assignment.get('document_request_keys') or {})
    keys[school] = key
    assignment['document_request_keys'] = keys
    if school == assignment['school']:
        assignment['document_request_key'] = key
    journal.put('audit_assignment', assignment['claim'], assignment)


def request(store, journal, payload):
    """向一笔派工对应的学校索件；可用 school 指定关联组中的其他学校。"""
    with journal.transaction():
        assignment = _assignment(journal, payload['claim'])
        if assignment.get('status') == 'recused':
            raise DataError('assignment recused; reassign before requesting documents')
        group = _group(store, journal, assignment['claim'])
        school = payload.get('school', assignment['school'])
        if not isinstance(school, str) or not school:
            raise DataError('school required')
        return _request_for_school(store, journal, assignment, group, school)


def request_group(store, journal, payload):
    """按学校逐一索件，返回每校独立的材料请求。"""
    with journal.transaction():
        assignment = _assignment(journal, payload['claim'])
        if assignment.get('status') == 'recused':
            raise DataError('assignment recused; reassign before requesting documents')
        group = _group(store, journal, assignment['claim'])
        group_claims = (sorted(set(group['open_claims']) | {assignment['claim']})
                        if group else [assignment['claim']])
        claim_rows = [store.find('claims', claim=claim) for claim in group_claims]
        schools = sorted({row['school'] for row in claim_rows})
        return {'claim': assignment['claim'],
                'group_id': group['group_id'] if group else None,
                'requests': [_request_for_school(store, journal, assignment, group, school)
                             for school in schools],
                'privacy_note': ('各校只收到本校签到明细；跨校关系只给出汇总数量，'
                                 '不发送其他学校的个人签到记录。')}
