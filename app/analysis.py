from .domain import claim_check
from .storage import integer


def checks(store,query):
    return {'claims':[claim_check(store,row) for row in store.table('claims')]}


def queue(store,query):
    policy=store.find('policies',policy='current')
    budget=integer(query,'budget',policy['daily_budget'],1,100)
    rows=sorted(store.table('claims'),key=lambda row:(-row['amount'],row['claim']))
    return {'selected':[claim_check(store,row) for row in rows[:budget]],
            'policy':'descending_amount','budget':budget,
            'reviewed_claims':len(store.table('reviews'))}


ROUTES={'/api/checks':checks,'/api/queue':queue}
