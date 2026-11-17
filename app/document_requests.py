def request(store,journal,payload):
    with journal.transaction():
        assignment=journal.get('audit_assignment',payload['claim'])
        records=store.select('presence',claim=assignment['claim'])
        row=dict(claim=assignment['claim'],school=assignment['school'],
                 requested_records=[record['record'] for record in records],status='requested')
        return journal.put('document_request',row['claim'],row)
