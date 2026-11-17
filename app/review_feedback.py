from .storage import DataError


def resolve(store,journal,payload):
    with journal.transaction():
        assignment=journal.get('audit_assignment',payload['claim'])
        if assignment['reviewer']!=payload['reviewer']:
            raise DataError('wrong reviewer')
        if payload['outcome'] not in ('supported','needs_correction','inconclusive'):
            raise DataError('invalid review outcome')
        document=journal.get('document_request',payload['claim'])
        result=dict(claim=assignment['claim'],reviewer=assignment['reviewer'],
                    outcome=payload['outcome'],selection='manual',requested_records=document['requested_records'])
        journal.put('audit_result',result['claim'],result)
        assignment['status']='reviewed'
        journal.put('audit_assignment',assignment['claim'],assignment)
        return result
