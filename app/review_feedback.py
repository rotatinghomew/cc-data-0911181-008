from datetime import datetime, timezone

from .storage import DataError

OUTCOMES = ('supported', 'needs_correction', 'inconclusive')


def _request_for(journal, assignment):
    """结案使用派工学校自己的材料请求，不误用其他学校的索件。"""
    keys = assignment.get('document_request_keys') or {}
    candidates = []
    if assignment['school'] in keys:
        candidates.append(keys[assignment['school']])
    if assignment.get('document_request_key'):
        candidates.append(assignment['document_request_key'])
    for key in candidates:
        try:
            row = journal.get('document_request', key)
        except DataError:
            continue
        if assignment['claim'] in row.get('claims', [row.get('claim')]):
            return row
    for row in journal.all('document_request'):
        if row.get('school') == assignment['school'] and \
                assignment['claim'] in row.get('claims', [row.get('claim')]):
            return row
    raise DataError('document request required')


def resolve(store, journal, payload):
    """登记单笔复核结论；只结这一笔，不连带结案同组其他班次。"""
    with journal.transaction():
        assignment = journal.get('audit_assignment', payload['claim'])
        if assignment['reviewer'] != payload['reviewer']:
            raise DataError('wrong reviewer')
        if assignment.get('status') == 'recused':
            raise DataError('assignment recused')
        if assignment.get('status') == 'reviewed':
            raise DataError('claim already reviewed')
        if payload['outcome'] not in OUTCOMES:
            raise DataError('invalid review outcome')
        document = _request_for(journal, assignment)
        result = dict(claim=assignment['claim'],
                      reviewer=assignment['reviewer'],
                      outcome=payload['outcome'],
                      selection=assignment.get('selection', 'manual'),
                      group_id=assignment.get('group_id'),
                      relation_basis=assignment.get('relation_basis', []),
                      requested_records=document['requested_records'],
                      reviewed_at=datetime.now(timezone.utc).date().isoformat())
        journal.put('audit_result', result['claim'], result)
        assignment['status'] = 'reviewed'
        assignment['reviewed_at'] = result['reviewed_at']
        journal.put('audit_assignment', assignment['claim'], assignment)
        document['status'] = 'fulfilled'
        resolved = set(document.get('resolved_claims', [])) | {assignment['claim']}
        document['resolved_claims'] = sorted(resolved)
        journal.put('document_request', document['request_key'], document)
        return result
