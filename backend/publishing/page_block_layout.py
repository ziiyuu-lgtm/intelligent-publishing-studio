"""Optional responsive grid placement for independent page blocks."""
from django import forms
from django.utils.translation import gettext_lazy as _

GRID_COLUMNS = 12
GRID_ALIGNMENTS = (("start", _("Align to top")), ("stretch", _("Match row height")))


def validate_block_layout(value):
    if not isinstance(value, dict) or set(value) != {"gap", "alignment"}:
        raise ValueError("invalid grid fields")
    if type(value["gap"]) is not int or not 0 <= value["gap"] <= 64:
        raise ValueError("invalid grid gap")
    if not isinstance(value["alignment"], str) or value["alignment"] not in dict(GRID_ALIGNMENTS):
        raise ValueError("invalid grid alignment")
    return dict(value)


def validate_grid_span(value):
    if type(value) is not int or not 1 <= value <= GRID_COLUMNS:
        raise ValueError("invalid grid span")
    return value


class GridSpanForm(forms.Form):
    grid_span = forms.IntegerField(label=_("Grid width (1–12 columns)"), required=False, min_value=1, max_value=GRID_COLUMNS,
        help_text=_("Leave blank for full width. Use 6 for half, 4 for one third, or 8 for two thirds."),
        widget=forms.NumberInput(attrs={"placeholder":"12"}))

    def clean(self):
        data = super().clean()
        if "grid_span" in self.initial and self.add_prefix("grid_form_present") not in self.data:
            raise forms.ValidationError(_("Reload the block editor before changing grid placement."))
        return data


class BlockLayoutForm(forms.Form):
    enabled = forms.BooleanField(label=_("Arrange blocks side by side in a responsive grid"), required=False)
    gap = forms.IntegerField(label=_("Gap between blocks (px)"), required=False, min_value=0, max_value=64)
    alignment = forms.ChoiceField(label=_("Alignment within a row"), required=False,
        choices=(("", _("Choose alignment")), *GRID_ALIGNMENTS))

    def __init__(self, *args, saved_layout=None, **kwargs):
        self.saved_layout = saved_layout
        if saved_layout is not None:
            kwargs.setdefault("initial", {"enabled":True, **saved_layout})
        else:
            kwargs.setdefault("initial", {"gap":24, "alignment":"start"})
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        if self.saved_layout is not None and self.add_prefix("form_present") not in self.data:
            raise forms.ValidationError(_("Reload the page editor before changing its grid layout."))
        if data.get("enabled"):
            try:
                self.layout = validate_block_layout({"gap":data.get("gap"), "alignment":data.get("alignment")})
            except ValueError as exc:
                raise forms.ValidationError(_("Choose a valid block gap and row alignment.")) from exc
        else:
            self.layout = None
        return data
