"""Author account updates in the local prototype; no external delivery."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .author_notices import visible_author_notices
from .models import AuthorNotice
from .views import _author_can_view


@login_required
@never_cache
@require_GET
def author_notice_inbox(request):
    notices = visible_author_notices(request.user).order_by("-created_at", "-pk")
    unread_count = notices.filter(read_at__isnull=True).count()
    selected = request.GET.get("filter", "all")
    if selected not in ("all", "unread"):
        selected = "all"
    if selected == "unread":
        notices = notices.filter(read_at__isnull=True)
    return render(request, "publishing/author_notice_inbox.html", {
        "page":Paginator(notices, 20).get_page(request.GET.get("page")),
        "selected":selected, "unread_count":unread_count,
    })


@login_required
@never_cache
@require_POST
def read_author_notice(request, pk):
    with transaction.atomic():
        notice = get_object_or_404(AuthorNotice.objects.select_for_update().filter(
            pk__in=visible_author_notices(request.user).values("pk")).select_related("submission__journal__organisation"), pk=pk)
        if not _author_can_view(request.user, notice.submission):
            from django.http import Http404
            raise Http404
        if notice.read_at is None:
            notice.read_at = timezone.now()
            notice.save(update_fields=["read_at"])
    messages.success(request, _("Notice marked as read in this local test account."))
    return redirect("author_notice_inbox")
