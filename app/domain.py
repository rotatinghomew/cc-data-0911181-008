def claim_check(store,claim):
    records=store.select('presence',claim=claim['claim'])
    reviews=store.select('reviews',claim=claim['claim'])
    issues=[]
    if not claim['attachments']:
        issues.append('missing_attachment')
    if claim['amount']<0:
        issues.append('negative_amount')
    return dict(claim=claim['claim'],amount=claim['amount'],school=claim['school'],
                document_issues=issues,presence_records=len(records),
                unique_rollcall_people=len({row['person'] for row in records}),
                decision=reviews[0]['outcome'] if reviews else 'unreviewed')
