"""Read-only comparison of author-supplied metadata captured for manuscript versions."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .views import _author_can_view, _editor_can_view, _submission


def _selection(request, key, versions, default):
    supplied = request.GET.getlist(key)
    if not supplied:
        return default
    value = supplied[0]
    if len(supplied) != 1 or not value.isascii() or not value.isdigit() or len(value) > 9:
        raise Http404
    selected = next((version for version in versions if version.number == int(value)), None)
    if selected is None:
        raise Http404
    return selected


def _state(left, right, field):
    before, after = getattr(left, field), getattr(right, field)
    if before is None or after is None:
        return 'unknown'
    return 'unchanged' if before == after else 'changed'


@login_required
@never_cache
@require_GET
def compare_version_metadata(request, pk):
    submission = _submission(pk)
    if not (_author_can_view(request.user, submission) or _editor_can_view(request.user, submission)):
        raise Http404
    versions = list(submission.versions.order_by('-number'))
    if not versions:
        raise Http404
    left = _selection(request, 'before', versions, versions[1] if len(versions) > 1 else versions[0])
    right = _selection(request, 'after', versions, versions[0])
    return render(request, 'publishing/version_metadata_compare.html', {
        'submission':submission, 'versions':versions, 'before':left, 'after':right,
        'title_state':_state(left, right, 'title_snapshot'),
        'abstract_state':_state(left, right, 'abstract_snapshot'),
    })
