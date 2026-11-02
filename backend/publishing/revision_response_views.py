"""Private, author-written revision responses for fictional local submissions."""
import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as lazy_
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import RevisionPlanRun, RevisionResponseDraft, Submission
from .revision_plan_views import _current_decision
from .views import _author_can_view, _event, _submission

MAX_POINTS = 10
MAX_RESPONSE_LENGTH = 20000


class ResponsePointForm(forms.Form):
    comment = forms.CharField(label=lazy_("Comment to address"), max_length=2000, required=False,
                              widget=forms.Textarea(attrs={"rows": 3}))
    response = forms.CharField(label=lazy_("Your response"), max_length=5000, required=False,
                               widget=forms.Textarea(attrs={"rows": 4}))
    location = forms.CharField(label=lazy_("Location in revised manuscript (optional)"), max_length=250, required=False)

    def clean(self):
        data = super().clean()
        if (data.get("response") or data.get("location")) and not data.get("comment"):
            self.add_error("comment", lazy_("Enter the comment for this point."))
        return data


ResponsePointFormSet = forms.formset_factory(ResponsePointForm, extra=0, can_delete=True,
    max_num=MAX_POINTS, absolute_max=MAX_POINTS, validate_max=True)


def _source_for(user, submission, decision, raw_id):
    try:
        source_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError, AttributeError):
        raise Http404
    run = get_object_or_404(RevisionPlanRun, pk=source_id, requested_by=user, submission=submission,
                            decision=decision, status=RevisionPlanRun.Status.COMPLETED)
    if not isinstance(run.result, list) or len(run.result) > 5:
        raise Http404
    result = []
    for item in run.result:
        if not isinstance(item, dict) or not isinstance(item.get("evidence"), str) or not item["evidence"].strip() or len(item["evidence"]) > 200:
            raise Http404
        result.append({"comment": item["evidence"], "response": "", "location": ""})
    return run, result


def _valid_entries(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_POINTS:
        return False
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"comment", "response", "location"}:
            return False
        if not all(isinstance(item[key], str) for key in item):
            return False
        if not item["comment"].strip() or len(item["comment"]) > 2000 or len(item["response"]) > 5000 or len(item["location"]) > 250:
            return False
    return True


def compose_response(entries):
    """Return only author-entered words; never generate assertions about completed work."""
    if not _valid_entries(entries) or any(not item["response"].strip() for item in entries):
        return None
    sections = []
    for index, item in enumerate(entries, 1):
        section = f"{_('Comment')} {index}:\n{item['comment']}\n\n{_('Author response')}:\n{item['response']}"
        if item["location"].strip():
            section += f"\n\n{_('Location in revised manuscript')}:\n{item['location']}"
        sections.append(section)
    result = "\n\n---\n\n".join(sections)
    return result if len(result) <= MAX_RESPONSE_LENGTH else None


def response_prefill(request, submission):
    """Return a verified snapshot for the revision form, or None without an input id."""
    raw_id = request.GET.get("response_draft")
    if not raw_id:
        return None
    try:
        draft_id = uuid.UUID(raw_id)
    except (ValueError, TypeError, AttributeError):
        raise Http404
    decision = _current_decision(submission)
    draft = get_object_or_404(RevisionResponseDraft, pk=draft_id, submission=submission, author=request.user)
    if (not decision or draft.decision_id != decision.pk or request.GET.get("revision") != str(draft.revision)):
        raise ValueError("stale")
    if draft.source_run_id:
        _source_for(request.user, submission, decision, draft.source_run_id)
    response = compose_response(draft.entries)
    if response is None:
        raise ValueError("incomplete")
    return response


def _context(submission, decision, draft, formset, source, *, message=""):
    entries = draft.entries if draft and _valid_entries(draft.entries) else []
    response = compose_response(entries) if entries else None
    return {"submission": submission, "decision": decision, "draft": draft, "formset": formset,
            "draft_revision": draft.revision if draft else 0, "source_run": source,
            "saved_entries": entries, "completed_count": sum(bool(item["response"].strip()) for item in entries),
            "preview_response": response, "message": message}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def revision_response_builder(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    decision = _current_decision(submission)
    if not decision:
        return HttpResponse(_("A current revision decision is required."), status=409)
    draft = RevisionResponseDraft.objects.filter(submission=submission, decision=decision, author=request.user).first()
    if draft and not _valid_entries(draft.entries):
        return HttpResponse(_("This response draft is unavailable; contact the local administrator."), status=409)
    raw_source = request.GET.get("plan") if request.method == "GET" else request.POST.get("source_run")
    source, imported = _source_for(request.user, submission, decision, raw_source) if raw_source else (None, [])
    if draft and draft.source_run_id:
        saved_source = _source_for(request.user, submission, decision, draft.source_run_id)[0]
    else:
        saved_source = None
    if request.method == "GET":
        initial = draft.entries if draft else (imported or [{}])
        msg = _("An existing draft was kept; the selected plan was not imported.") if draft and source and source.pk != draft.source_run_id else ""
        return render(request, "publishing/revision_response_builder.html", _context(submission, decision, draft,
            ResponsePointFormSet(prefix="point", initial=initial), saved_source if draft else source, message=msg))
    if request.POST.get("decision_id") != str(decision.pk):
        return HttpResponse(_("The revision decision changed; reload the page."), status=409)
    if draft and (str(draft.source_run_id or "") != str(source.pk if source else "")):
        return HttpResponse(_("The response draft changed; reload the page."), status=409)
    try:
        total = int(request.POST.get("point-TOTAL_FORMS", "-1"))
        expected = int(request.POST.get("draft_revision", "-1"))
    except (ValueError, TypeError):
        return HttpResponse(_("The response form is invalid."), status=400)
    if not 1 <= total <= MAX_POINTS or str(total) != request.POST.get("point-TOTAL_FORMS") or expected < 0:
        return HttpResponse(_("The response form is invalid."), status=400)
    if expected != (draft.revision if draft else 0):
        return HttpResponse(_("The response draft changed; reload the page."), status=409)
    formset = ResponsePointFormSet(request.POST, prefix="point")
    if not formset.is_valid():
        return render(request, "publishing/revision_response_builder.html", _context(submission, decision, draft, formset, source), status=400)
    entries = [{key: form.cleaned_data.get(key, "") for key in ("comment", "response", "location")}
               for form in formset if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("comment")]
    if request.POST.get("action") == "add":
        initial = entries or [{}]
        if len(initial) >= MAX_POINTS:
            return render(request, "publishing/revision_response_builder.html", _context(submission, decision, draft, formset, source,
                message=_("The maximum number of points has been reached.")), status=400)
        return render(request, "publishing/revision_response_builder.html", _context(submission, decision, draft,
            ResponsePointFormSet(prefix="point", initial=[*initial, {}]), source, message=_("New point added. Save your draft to keep your edits.")))
    if request.POST.get("action") != "save" or not _valid_entries(entries):
        return render(request, "publishing/revision_response_builder.html", _context(submission, decision, draft, formset, source,
            message=_("Add at least one comment before saving.")), status=400)
    with transaction.atomic():
        current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(request.user, current):
            raise Http404
        current_decision = _current_decision(current)
        if not current_decision or current_decision.pk != decision.pk:
            return HttpResponse(_("The revision decision changed; reload the page."), status=409)
        if source:
            _source_for(request.user, current, current_decision, source.pk)
        locked = RevisionResponseDraft.objects.select_for_update().filter(submission=current, decision=current_decision, author=request.user).first()
        if expected != (locked.revision if locked else 0) or (locked and str(locked.source_run_id or "") != str(source.pk if source else "")):
            return HttpResponse(_("The response draft changed; reload the page."), status=409)
        if locked is None:
            RevisionResponseDraft.objects.create(submission=current, decision=current_decision, author=request.user, source_run=source, entries=entries)
            _event(current, request.user, "revision_response_draft_saved")
        elif locked.entries != entries:
            locked.entries, locked.revision = entries, locked.revision + 1
            locked.save(update_fields=["entries", "revision", "updated_at"])
            _event(current, request.user, "revision_response_draft_saved")
    return redirect("revision_response_builder", pk=pk)
