"""Bind a revision form to one author, decision, round, version and file policy."""
from django.core import signing

from .revision_plan_views import _current_decision

TOKEN_SALT = "publishing.revision-form.v1"
TOKEN_MAX_AGE = 7 * 24 * 60 * 60


def current_revision_decision(submission):
    decision = _current_decision(submission)
    if not decision:
        return None
    latest_round = submission.rounds.order_by("-number").first()
    if not latest_round or latest_round.pk != decision.round_id:
        return None
    return decision


def snapshot(submission, decision):
    return {"submission": str(submission.pk), "author": submission.author_id,
            "decision": decision.pk, "round": decision.round_id,
            "version": decision.round.version_id, "review_mode": submission.review_mode}


def sign_revision_form(submission, decision):
    return signing.dumps(snapshot(submission, decision), salt=TOKEN_SALT, compress=True)


def matches_revision_form(token, submission, decision):
    if not isinstance(token, str) or not token or not decision:
        return False
    try:
        supplied = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return False
    return supplied == snapshot(submission, decision)
