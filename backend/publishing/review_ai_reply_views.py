"""Local editorial inbox for historical review AI permission replies."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import ReviewAIConsent, ReviewAIReplyNotice
from .review_ai_reply_notices import visible_review_ai_replies
from .review_ai_requests import entry_current
from .review_synthesis_views import _state
from .views import _editor_can_view


def _enabled():
    if not settings.DEBUG:
        raise Http404


def _row(notice, actor, cache):
    permission_request = notice.permission_request
    key = (permission_request.submission_id, permission_request.review_round.number)
    if key not in cache:
        try:
            cache[key] = _state(actor, *key)
        except Http404:
            cache[key] = None
    state = cache[key]
    if state is None:
        raise Http404
    current = bool(state['round'].pk == permission_request.review_round_id and entry_current(permission_request))
    if not current:
        allowed = False
    elif permission_request.subject == ReviewAIConsent.Subject.AUTHOR:
        allowed = bool(state['author'])
    else:
        allowed = permission_request.assignment_id in state['reviewers']
    return {'notice':notice, 'current':current, 'allowed':allowed}


@login_required
@never_cache
@require_GET
def reply_inbox(request):
    _enabled()
    notices = visible_review_ai_replies(request.user)
    unread = notices.filter(read_at__isnull=True).count()
    selected = request.GET.get('filter', 'all')
    if selected not in ('all', 'unread'):
        selected = 'all'
    if selected == 'unread':
        notices = notices.filter(read_at__isnull=True)
    page = Paginator(notices, 20).get_page(request.GET.get('page'))
    cache = {}
    rows = []
    for notice in page.object_list:
        try:
            rows.append(_row(notice, request.user, cache))
        except Http404:
            # Access may have changed after the initial inbox query.
            continue
    return render(request, 'publishing/review_ai_reply_inbox.html', {
        'page':page, 'rows':rows, 'selected':selected, 'unread_count':unread})


@login_required
@never_cache
@require_GET
def reply_detail(request, pk):
    _enabled()
    notice = get_object_or_404(visible_review_ai_replies(request.user), pk=pk)
    return render(request, 'publishing/review_ai_reply_detail.html', _row(notice, request.user, {}))


@login_required
@never_cache
@require_POST
def mark_reply_read(request, pk):
    _enabled()
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        notice = get_object_or_404(ReviewAIReplyNotice.objects.select_for_update().filter(
            pk__in=visible_review_ai_replies(actor).values('pk')).select_related(
                'permission_request__submission__journal__organisation'), pk=pk)
        if not _editor_can_view(actor, notice.permission_request.submission):
            raise Http404
        if notice.read_at is None:
            notice.read_at = timezone.now()
            notice.save(update_fields=['read_at'])
    messages.success(request, _('Reply marked as read in this local test account.'))
    return redirect('review_ai_reply_inbox')
