from . import review_dispatch
from . import document_requests
from . import review_feedback

OPERATIONS={
    'review_dispatch.assign':review_dispatch.assign,
    'document_requests.request':document_requests.request,
    'review_feedback.resolve':review_feedback.resolve,
}
