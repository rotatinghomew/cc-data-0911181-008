from . import evaluation
from . import leads
from .domain import claim_check
from .storage import DataError, integer, single


def checks(store, query, journal=None):
    return {'claims': [claim_check(store, row) for row in store.table('claims')]}


def queue(store, query, journal=None):
    policy = store.find('policies', policy='current')
    budget = integer(query, 'budget', policy['daily_budget'], 1, 100)
    rows = sorted(store.table('claims'), key=lambda row: (-row['amount'], row['claim']))
    return {'selected': [claim_check(store, row) for row in rows[:budget]],
            'policy': 'descending_amount', 'budget': budget,
            'reviewed_claims': len(store.table('reviews'))}


def lead_groups(store, query, journal=None):
    scope = single(query, 'scope', 'all')
    if scope not in ('all', 'open'):
        raise DataError('invalid scope: ' + str(scope))
    return leads.model(store, journal, scope)


def lead_plan(store, query, journal=None):
    return leads.plan(store, query, journal)


def lead_evaluation(store, query, journal=None):
    return evaluation.compare(store, query, journal)


ROUTES = {
    '/api/checks': checks,
    '/api/queue': queue,
    '/api/leads': lead_groups,
    '/api/plan': lead_plan,
    '/api/leads/evaluation': lead_evaluation,
}
