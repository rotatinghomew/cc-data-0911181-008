from .storage import DataError


def assign(store,journal,payload):
    claim=store.find('claims',claim=payload['claim'])
    reviewer=store.find('reviewers',reviewer=payload['reviewer'])
    if claim['school'] in reviewer['conflicts']:
        raise DataError('reviewer conflict of interest')
    with journal.transaction():
        existing=journal.all('audit_assignment')
        if any(row['claim']==claim['claim'] for row in existing):
            return next(row for row in existing if row['claim']==claim['claim'])
        if sum(row['reviewer']==reviewer['reviewer'] for row in existing)>=reviewer['capacity']:
            raise DataError('reviewer capacity exceeded')
        return journal.put('audit_assignment',claim['claim'],dict(claim=claim['claim'],school=claim['school'],
                           reviewer=reviewer['reviewer'],status='assigned'))
