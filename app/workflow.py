from . import review_dispatch
from . import document_requests
from . import review_feedback

OPERATIONS={
    'review_dispatch.assign':review_dispatch.assign,
    'review_dispatch.dispatch':review_dispatch.dispatch,
    'review_dispatch.recuse':review_dispatch.recuse,
    'document_requests.request':document_requests.request,
    'document_requests.request_group':document_requests.request_group,
    'review_feedback.resolve':review_feedback.resolve,
}
