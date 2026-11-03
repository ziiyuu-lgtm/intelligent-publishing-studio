from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (Journal, JournalSiteDraft, JournalSiteRelease, Membership, Organisation,
    OrganisationMembership, OrganisationPortalDraft, OrganisationPortalEvent, OrganisationPortalRelease, Submission)
from publishing.site_design import sample_by_id


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublisherPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("portal-manager")
        cls.other_manager = users.create_user("other-publisher-manager")
        cls.journal_manager = users.create_user("journal-manager")
        cls.author = users.create_user("portal-test-author")
        cls.admin = users.create_superuser("portal-test-admin", password="long-synthetic-password")
        cls.org = Organisation.objects.create(name="Fictional publisher A", slug="publisher-a")
        cls.other_org = Organisation.objects.create(name="Fictional publisher B", slug="publisher-b")
        cls.a = Journal.objects.create(organisation=cls.org, name="Fictional Journal A", slug="portal-journal-a")
        cls.b = Journal.objects.create(organisation=cls.org, name="Fictional Journal B", slug="portal-journal-b")
        cls.foreign = Journal.objects.create(organisation=cls.other_org, name="Foreign Journal", slug="portal-journal-c")
        cls.inactive = Journal.objects.create(organisation=cls.org, name="Inactive Journal", slug="portal-inactive", active=False)
        OrganisationMembership.objects.create(organisation=cls.org, user=cls.manager, role=OrganisationMembership.Role.PORTAL_MANAGER)
        OrganisationMembership.objects.create(organisation=cls.other_org, user=cls.other_manager, role=OrganisationMembership.Role.PORTAL_MANAGER)
        Membership.objects.create(journal=cls.a, user=cls.journal_manager, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(journal=cls.a, user=cls.author, role=Membership.Role.AUTHOR)
        cls.submission = Submission.objects.create(journal=cls.a, author=cls.author, title="PRIVATE test manuscript",
            abstract="PRIVATE abstract", manuscript="manuscripts/fake.pdf", status=Submission.Status.SUBMITTED)

    def url(self, name, *args, org=None):
        return reverse(name, args=[(org or self.org).slug, *args])

    def start(self, actor=None, org=None):
        self.client.force_login(actor or self.manager)
        response = self.client.post(self.url("start_publisher_portal", org=org), {"synthetic":"yes"})
        self.assertEqual(response.status_code, 302)
        return OrganisationPortalDraft.objects.get(organisation=org or self.org)

    def token(self, org=None):
        page = self.client.get(self.url("manage_publisher_portal", org=org))
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"]}

    def edit(self, site, journals=None, **overrides):
        data = {**self.token(site.organisation), "brand_name":"Fictional press", "headline":"A fictional publishing house",
                "tagline":"Research in a local test", "introduction":"Fictional publisher introduction.",
                "journals":journals if journals is not None else [self.a.pk, self.b.pk]}
        data.update(overrides)
        return self.client.post(self.url("edit_publisher_portal", org=site.organisation), data)

    def publish(self, site):
        data = {**self.token(site.organisation), "synthetic":"yes", "confirm":"yes"}
        return self.client.post(self.url("publish_publisher_portal", org=site.organisation), data)

    def publish_journal(self, journal):
        site = JournalSiteDraft.objects.create(journal=journal, brand_name=journal.name, headline="Fictional",
            introduction="Fictional", created_by=self.journal_manager)
        release = JournalSiteRelease.objects.create(site=site, revision=1, brand_name=journal.name, headline="Fictional",
            introduction="Fictional", theme="forest", layout="classic", published_by=self.journal_manager)
        site.current_release = release
        site.save(update_fields=["current_release"])

    def test_org_role_is_explicit_and_does_not_grant_manuscripts(self):
        url = self.url("manage_publisher_portal")
        for user in (self.other_manager, self.journal_manager, self.author, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(self.url("start_publisher_portal"), {}).status_code, 400)
        site = self.start()
        self.assertEqual(self.start().pk, site.pk)
        self.assertEqual(OrganisationPortalEvent.objects.filter(action="portal_draft_created").count(), 1)
        page = self.client.get(reverse("workspace"))
        self.assertContains(page, "Publisher portal design")
        self.assertNotContains(page, "PRIVATE test manuscript")
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(Client().get(self.url("preview_publisher_portal")).status_code, 302)
        OrganisationMembership.objects.filter(user=self.manager).update(active=False)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_local_feature_gate_blocks_management_and_public(self):
        self.start()
        for config in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with self.subTest(config=config), override_settings(**config):
                self.assertEqual(self.client.get(self.url("manage_publisher_portal")).status_code, 404)
                self.assertEqual(Client().get(self.url("public_publisher_portal")).status_code, 404)

    def test_selection_is_scoped_active_and_never_automatic(self):
        site = self.start()
        page = self.client.get(self.url("manage_publisher_portal"))
        self.assertContains(page, self.a.name)
        self.assertNotContains(page, self.foreign.name)
        self.assertNotContains(page, self.inactive.name)
        for invalid in ([self.a.pk, self.foreign.pk], [self.inactive.pk]):
            self.assertEqual(self.edit(site, invalid).status_code, 400)
        self.assertEqual(self.edit(site, [self.a.pk]).status_code, 302)
        preview = self.client.get(self.url("preview_publisher_portal"))
        self.assertContains(preview, self.a.name)
        self.assertNotContains(preview, self.b.name)
        self.assertNotContains(preview, self.foreign.name)
        self.assertNotContains(preview, "PRIVATE test manuscript")
        self.assertContains(preview, "Test journal site not yet published")
        self.assertNotContains(preview, reverse("public_journal_site", args=[self.a.slug]))
        self.assertIn("no-store", preview["Cache-Control"])

    def test_publication_freezes_text_and_names_with_live_journal_safety(self):
        site = self.start()
        self.assertEqual(self.edit(site, [self.a.pk, self.b.pk]).status_code, 302)
        self.publish_journal(self.a)
        self.assertEqual(Client().get(self.url("public_publisher_portal")).status_code, 404)
        self.assertNotContains(Client().get(reverse("home")), self.url("public_publisher_portal"))
        self.assertEqual(self.publish(site).status_code, 302)
        public = Client()
        self.assertContains(public.get(reverse("home")), self.url("public_publisher_portal"))
        page = public.get(self.url("public_publisher_portal"))
        self.assertContains(page, "A fictional publishing house")
        self.assertContains(page, self.a.name)
        self.assertContains(page, reverse("public_journal_site", args=[self.a.slug]))
        self.assertNotContains(page, reverse("public_journal_site", args=[self.b.slug]))
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertNotContains(page, "PRIVATE test manuscript")
        first = OrganisationPortalRelease.objects.get(site=site)
        Journal.objects.filter(pk=self.a.pk).update(name="Renamed canonical journal")
        self.assertContains(public.get(self.url("public_publisher_portal")), "Fictional Journal A")
        self.assertNotContains(public.get(self.url("public_publisher_portal")), "Renamed canonical journal")
        self.assertEqual(self.edit(site, [self.a.pk], headline="A new unpublished draft").status_code, 302)
        self.assertNotContains(public.get(self.url("public_publisher_portal")), "A new unpublished draft")
        self.assertEqual(self.publish(site).status_code, 302)
        self.assertContains(public.get(self.url("public_publisher_portal")), "A new unpublished draft")
        self.assertNotContains(public.get(self.url("public_publisher_portal")), self.b.name)
        response = self.client.post(self.url("restore_publisher_portal", first.pk), {**self.token(), "confirm":"yes"})
        self.assertEqual(response.status_code, 302)
        self.assertContains(public.get(self.url("public_publisher_portal")), "A new unpublished draft")
        restored_preview = self.client.get(self.url("preview_publisher_portal"))
        self.assertContains(restored_preview, "A fictional publishing house")
        self.assertContains(restored_preview, "Renamed canonical journal")
        self.assertEqual(first.journals[0]["name"], "Fictional Journal A")
        self.assertEqual(self.publish(site).status_code, 302)
        self.assertContains(public.get(self.url("public_publisher_portal")), "A fictional publishing house")
        self.assertEqual(OrganisationPortalEvent.objects.filter(action="portal_draft_restored", source_release=first).count(), 1)
        Journal.objects.filter(pk=self.a.pk).update(active=False)
        self.assertNotContains(public.get(self.url("public_publisher_portal")), "Renamed canonical journal")
        self.assertNotContains(public.get(self.url("public_publisher_portal")), "Fictional Journal A")

    def test_journal_links_follow_renderable_public_release_not_private_draft(self):
        site = self.start()
        self.edit(site, [self.a.pk])
        self.publish(site)
        public = Client()
        link = reverse("public_journal_site", args=[self.a.slug])
        self.assertNotContains(public.get(self.url("public_publisher_portal")), link)
        self.publish_journal(self.a)
        self.assertContains(public.get(self.url("public_publisher_portal")), link)
        journal_site = JournalSiteDraft.objects.get(journal=self.a)
        JournalSiteRelease.objects.filter(pk=journal_site.current_release_id).update(design_config={"bad":"design"})
        self.assertNotContains(public.get(self.url("public_publisher_portal")), link)
        JournalSiteRelease.objects.filter(pk=journal_site.current_release_id).update(design_config={}, sections_config=[{"bad":"module"}])
        self.assertNotContains(public.get(self.url("public_publisher_portal")), link)
        JournalSiteRelease.objects.filter(pk=journal_site.current_release_id).update(sections_config=[], theme="unknown")
        self.assertNotContains(public.get(self.url("public_publisher_portal")), link)

    def test_edit_and_publication_reject_stale_journal_and_confirmation(self):
        site = self.start()
        self.edit(site, [self.a.pk])
        stale = self.token()
        Journal.objects.filter(pk=self.a.pk).update(name="Updated title")
        self.assertEqual(self.client.post(self.url("publish_publisher_portal"),
            {**stale, "confirm":"yes", "synthetic":"yes"}).status_code, 409)
        token = self.token()
        self.assertEqual(self.client.post(self.url("publish_publisher_portal"),
            {**token, "confirm":"yes"}).status_code, 400)
        self.assertEqual(self.publish(site).status_code, 302)
        self.assertEqual(self.publish(site).status_code, 409)
        self.assertEqual(OrganisationPortalRelease.objects.count(), 1)

    def test_selection_limit_is_enforced(self):
        site = self.start()
        journals = [self.a.pk, self.b.pk]
        for index in range(49):
            journal = Journal.objects.create(organisation=self.org, name=f"Fictional extra {index}",
                                             slug=f"extra-{index}")
            journals.append(journal.pk)
        self.assertEqual(self.edit(site, journals).status_code, 400)
        self.assertEqual(self.edit(site, journals[:50]).status_code, 302)
        site.refresh_from_db()
        self.assertEqual(len(site.journal_ids), 50)

    def test_samples_and_custom_colours_are_draft_only(self):
        site = self.start()
        self.edit(site, [self.a.pk])
        original = OrganisationPortalDraft.objects.get(pk=site.pk)
        for sample in ("heritage", "editorial", "horizon", "frontier", "insight", "discovery"):
            page = self.client.get(self.url("preview_publisher_sample", sample))
            self.assertContains(page, "Private template preview")
        site.refresh_from_db()
        self.assertEqual(site.design_config, original.design_config)
        self.assertEqual(self.client.post(self.url("apply_publisher_sample", "frontier"),
            {**self.token(), "confirm":"yes"}).status_code, 302)
        site.refresh_from_db()
        self.assertEqual(site.design_config, sample_by_id("frontier")["config"])
        self.assertEqual(site.journal_ids, [self.a.pk])
        config = {**site.design_config, "primary":"#ae65cf", "section_order":"archive_first"}
        self.assertEqual(self.client.post(self.url("edit_publisher_appearance"), {**self.token(), **config}).status_code, 302)
        page = self.client.get(self.url("preview_publisher_portal"))
        self.assertContains(page, "--site-accent:#ae65cf")
        self.assertContains(page, "order-archive_first")
        self.assertFalse(OrganisationPortalRelease.objects.exists())
        bad = {**config, "primary":"javascript:alert(1)"}
        self.assertEqual(self.client.post(self.url("edit_publisher_appearance"), {**self.token(), **bad}).status_code, 400)

    def test_text_is_escaped_and_chinese_is_available(self):
        site = self.start()
        self.assertEqual(self.edit(site, [], brand_name="<svg onload=alert(1)>",
            headline="<script>alert(1)</script>", introduction="<img src=x>").status_code, 302)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(self.url("manage_publisher_portal")), "出版社门户设计")
        page = self.client.get(self.url("preview_publisher_portal"))
        self.assertContains(page, "&lt;script&gt;")
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertContains(page, "&lt;img src=x&gt;")
        self.assertContains(page, "出版社介绍")

    def test_cross_org_restore_invalid_release_and_inactive_journal(self):
        site = self.start()
        self.edit(site)
        self.publish(site)
        first = OrganisationPortalRelease.objects.get(site=site)
        self.start(self.other_manager, self.other_org)
        other_site = OrganisationPortalDraft.objects.get(organisation=self.other_org)
        self.assertEqual(self.client.post(self.url("restore_publisher_portal", first.pk, org=self.other_org),
            {**self.token(self.other_org), "confirm":"yes"}).status_code, 404)
        OrganisationPortalDraft.objects.filter(pk=other_site.pk).update(current_release=first)
        self.assertEqual(Client().get(self.url("public_publisher_portal", org=self.other_org)).status_code, 404)
        self.client.force_login(self.manager)
        Journal.objects.filter(pk=self.a.pk).update(active=False)
        self.assertEqual(self.client.post(self.url("restore_publisher_portal", first.pk),
            {**self.token(), "confirm":"yes"}).status_code, 409)

    def test_csrf_is_required_for_changes(self):
        self.start()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(self.url("start_publisher_portal"), {"synthetic":"yes"}).status_code, 403)
        self.assertEqual(client.post(self.url("publish_publisher_portal"),
            {"revision":"1", "fingerprint":"x", "confirm":"yes", "synthetic":"yes"}).status_code, 403)
