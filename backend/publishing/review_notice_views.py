"""Local account notices; no mail, scheduler, external service or message delivery."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import ReviewNotice
from .review_notices import visible_review_notices
from .views import _review


@login_required
@never_cache
@require_GET
def review_notice_inbox(request):
    notices = visible_review_notices(request.user).order_by("-created_at", "-pk")
    unread_count = notices.filter(read_at__isnull=True).count()
    selected = request.GET.get("filter", "all")
    if selected not in ("all", "unread"):
        selected = "all"
    if selected == "unread":
        notices = notices.filter(read_at__isnull=True)
    page = Paginator(notices, 20).get_page(request.GET.get("page"))
    return render(request, "publishing/review_notice_inbox.html", {
        "page":page, "selected":selected, "unread_count":unread_count,
    })


@login_required
@never_cache
@require_POST
def read_review_notice(request, pk):
    with transaction.atomic():
        notice = get_object_or_404(ReviewNotice.objects.select_for_update().filter(
            pk__in=visible_review_notices(request.user).values("pk")), pk=pk)
        # Recheck against the canonical task access before changing its read state.
        _review(notice.assignment_id, request.user)
        if notice.read_at is None:
            notice.read_at = timezone.now()
            notice.save(update_fields=["read_at"])
    messages.success(request, _("Notice marked as read in this local test account."))
    return redirect("review_notice_inbox")
