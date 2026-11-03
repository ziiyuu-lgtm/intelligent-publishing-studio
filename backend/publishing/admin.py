from django.contrib import admin
from .models import Journal, Membership, Organisation, OrganisationMembership


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Journal)
class JournalAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation", "slug", "review_mode", "allow_openai_metadata", "active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "journal", "role", "active")
    list_filter = ("journal", "role", "active")
    autocomplete_fields = ("user",)


@admin.register(OrganisationMembership)
class OrganisationMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organisation", "role", "active")
    list_filter = ("organisation", "role", "active")
    autocomplete_fields = ("user",)


# Unpublished manuscripts and review reports are intentionally not registered
# with the admin site: journal editor access must go through scoped views.
