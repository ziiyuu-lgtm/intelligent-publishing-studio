"""Private, ordered contributor metadata for fictional submission drafts."""
import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _

MAX_CONTRIBUTORS = 100
FIELDS = {"name", "affiliation", "email", "orcid", "corresponding"}
ORCID = re.compile(r"^(?:https://orcid\.org/)?([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9Xx])$")


def clean_orcid(value):
    if not isinstance(value, str):
        raise ValueError("invalid ORCID")
    value = value.strip()
    if not value:
        return ""
    match = ORCID.fullmatch(value)
    if not match:
        raise ValueError("invalid ORCID")
    code = match.group(1).upper()
    digits = code.replace("-", "")
    total = 0
    for char in digits[:15]:
        total = (total + int(char)) * 2
    result = (12 - total % 11) % 11
    if digits[-1] != ("X" if result == 10 else str(result)):
        raise ValueError("invalid ORCID checksum")
    return code


def validate_contributors(value):
    """Validate stored metadata without authenticating people or identifiers."""
    if not isinstance(value, list) or len(value) > MAX_CONTRIBUTORS:
        raise ValueError("invalid contributor count")
    result, emails, orcids = [], set(), set()
    for item in value:
        if not isinstance(item, dict) or set(item) != FIELDS or type(item["corresponding"]) is not bool:
            raise ValueError("invalid contributor fields")
        if not all(isinstance(item[k], str) for k in ("name", "affiliation", "email", "orcid")):
            raise ValueError("invalid contributor values")
        name, affiliation, email = (item[k].strip() for k in ("name", "affiliation", "email"))
        if (not name or len(name) > 200 or len(affiliation) > 500 or len(email) > 254 or
                any(ord(c) < 32 or ord(c) == 127 for c in name) or
                any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in affiliation)):
            raise ValueError("invalid contributor text")
        if email:
            try:
                validate_email(email)
            except ValidationError as exc:
                raise ValueError("invalid email") from exc
            key = email.casefold()
            if key in emails:
                raise ValueError("duplicate email")
            emails.add(key)
        orcid = clean_orcid(item["orcid"])
        if orcid:
            if orcid in orcids:
                raise ValueError("duplicate ORCID")
            orcids.add(orcid)
        result.append({"name":name, "affiliation":affiliation, "email":email,
                       "orcid":orcid, "corresponding":item["corresponding"]})
    return result


def ready_to_submit(contributors):
    """Preserve legacy empty lists; populated lists need complete contact details."""
    return (not contributors or
            (any(person["corresponding"] for person in contributors) and
             all(person["email"] for person in contributors if person["corresponding"])))


class ContributorForm(forms.Form):
    name = forms.CharField(label=_("Author name"), max_length=200)
    affiliation = forms.CharField(label=_("Affiliation"), max_length=500, required=False,
                                  widget=forms.Textarea(attrs={"rows":2}))
    email = forms.EmailField(label=_("Email address"), max_length=254, required=False)
    orcid = forms.CharField(label=_("ORCID iD (optional)"), max_length=50, required=False,
                            help_text=_("Enter an ORCID iD or its https://orcid.org/ address. It is not authenticated here."))
    corresponding = forms.BooleanField(label=_("Corresponding author"), required=False)

    def clean_orcid(self):
        try:
            return clean_orcid(self.cleaned_data["orcid"])
        except ValueError as exc:
            raise forms.ValidationError(_("Enter a correctly formatted ORCID iD with a valid check digit.")) from exc


class BaseContributorFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = []
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if not data or data.get("DELETE"):
                continue
            order = data.get("ORDER")
            person = {field:data[field] for field in FIELDS}
            rows.append(((order is None, order if order is not None else 0, index), person))
        try:
            self.cleaned_contributors = validate_contributors([person for _, person in sorted(rows, key=lambda row:row[0])])
        except ValueError as exc:
            raise forms.ValidationError(_("Check the author details. Email addresses and ORCID iDs must not be repeated.")) from exc


ContributorFormSet = formset_factory(ContributorForm, formset=BaseContributorFormSet, extra=1,
    can_order=True, can_delete=True, max_num=MAX_CONTRIBUTORS, absolute_max=MAX_CONTRIBUTORS * 2, validate_max=True)


class ContributorChangeForm(forms.Form):
    reason = forms.CharField(label=_("Reason for the requested change"), max_length=2000,
                             widget=forms.Textarea(attrs={"rows":4}))
    acknowledgement = forms.BooleanField(label=_(
        "I understand that this request does not notify authors or collect their agreement, and that editorial review is required."))


class ContributorChangeDecisionForm(forms.Form):
    outcome = forms.ChoiceField(label=_("Editorial outcome"), choices=[
        ("", _("Choose an outcome")), ("approved", _("Approve change")), ("declined", _("Decline change"))])
    note = forms.CharField(label=_("Decision note for the submitting author"), max_length=2000,
                           widget=forms.Textarea(attrs={"rows":3}))
    verified = forms.BooleanField(required=False, label=_(
        "For approval, I confirm I have reviewed documented agreement from all original and proposed authors and assessed potential conflicts. Evidence is retained separately."))

    def clean(self):
        data = super().clean()
        if data.get("outcome") == "approved" and not data.get("verified"):
            self.add_error("verified", _("Confirm the manual checks before approving the change."))
        return data
