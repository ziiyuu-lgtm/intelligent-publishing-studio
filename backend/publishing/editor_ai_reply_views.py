"""Local-only editorial inbox for replies; no external messages are sent."""
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

from .editor_ai_reply_notices import visible_editor_ai_reply_notices
from .editor_comparison_consent import active_consent, source_fingerprint
from .editor_revision_views import current_revision_package
from .models import EditorAIPermissionReplyNotice
from .views import _editor_can_view


def _enabled():
    if not settings.DEBUG:
        raise Http404


@login_required
@never_cache
@require_GET
def editor_ai_reply_inbox(request):
    _enabled()
    notices = visible_editor_ai_reply_notices(request.user).order_by('-created_at', '-pk')
    unread_count = notices.filter(read_at__isnull=True).count()
    selected = request.GET.get('filter', 'all')
    if selected not in ('all', 'unread'):
        selected = 'all'
    if selected == 'unread':
        notices = notices.filter(read_at__isnull=True)
    return render(request, 'publishing/editor_ai_reply_inbox.html', {
        'page': Paginator(notices, 20).get_page(request.GET.get('page')),
        'selected': selected, 'unread_count': unread_count,
    })


@login_required
@never_cache
@require_GET
def editor_ai_reply_detail(request, pk):
    _enabled()
    notice = get_object_or_404(visible_editor_ai_reply_notices(request.user), pk=pk)
    permission_request = notice.permission_request
    submission = permission_request.submission
    is_current = False
    allowed = False
    try:
        current, decision = current_revision_package(request.user, submission.pk)
    except Http404:
        pass
    else:
        is_current = (current.pk == permission_request.review_round_id and
            current.version_id == permission_request.version_id and decision.pk == permission_request.decision_id and
            source_fingerprint(current, decision) == permission_request.source_fingerprint)
        allowed = bool(is_current and active_consent(current, decision))
    return render(request, 'publishing/editor_ai_reply_detail.html', {
        'notice': notice, 'permission_request': permission_request, 'submission': submission,
        'is_current': is_current, 'currently_allowed': allowed,
    })


@login_required
@never_cache
@require_POST
def read_editor_ai_reply(request, pk):
    _enabled()
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        notice = get_object_or_404(EditorAIPermissionReplyNotice.objects.select_for_update().filter(
            pk__in=visible_editor_ai_reply_notices(user).values('pk')).select_related(
                'permission_request__submission__journal__organisation'), pk=pk)
        if not _editor_can_view(user, notice.permission_request.submission):
            raise Http404
        if notice.read_at is None:
            notice.read_at = timezone.now()
            notice.save(update_fields=['read_at'])
    messages.success(request, _('Reply marked as read in this local test account.'))
    return redirect('editor_ai_reply_inbox')
