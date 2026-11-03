from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from publishing.models import JournalSiteDraft, Membership, OrganisationMembership
from publishing.portal_sections import default_sections as portal_sections
from publishing.site_sections import default_sections as journal_sections
from publishing.site_design import sample_by_id
from . import test_site_images as helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SiteChangeReviewTests(TestCase):
    setUpTestData = helpers.SiteImageTests.__dict__['setUpTestData']
    owner = helpers.SiteImageTests.owner
    route = helpers.SiteImageTests.route
    login = helpers.SiteImageTests.login
    token = helpers.SiteImageTests.token
    publish = helpers.SiteImageTests.publish
    upload = helpers.SiteImageTests.upload

    def review(self, kind, slug=None):
        return reverse('journal_site_change_review' if kind == 'journal' else 'publisher_site_change_review',
                       args=[slug or self.owner(kind)[1]])

    def test_first_publication_and_unchanged_release_are_distinct(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                page = self.client.get(self.review(kind))
                self.assertEqual(page.status_code,200)
                self.assertContains(page,'No published test version yet')
                self.assertContains(page,'no published baseline to compare')
                self.assertNotContains(page,'No differences were found')
                self.assertIn('no-store',page['Cache-Control'])
                self.assertEqual(page['X-Robots-Tag'],'noindex, nofollow, noarchive')
                self.assertContains(self.client.get(self.route(kind,'manage')),self.review(kind))
                self.assertEqual(self.publish(kind).status_code,302)
                page = self.client.get(self.review(kind))
                self.assertContains(page,'No differences were found')
                self.assertEqual(page.context['change_count'],0)
                self.assertContains(page,self.route(kind,'preview'))
                self.assertContains(page,self.route(kind,'public'))

    def test_text_visual_hero_and_content_changes_are_scoped_and_read_only(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.assertEqual(self.publish(kind).status_code,302)
                manager, slug, site = self.owner(kind); site.refresh_from_db()
                site.headline = '<script>Fictional new headline</script>'
                site.tagline = 'A new fictional tagline'
                site.design_config = sample_by_id('frontier')['config']
                site.sections_config = journal_sections(site) if kind == 'journal' else portal_sections(site)
                site.pages_config = [{'slug':'for-authors','title':'For authors','body':'Fictional guidance','visible':True}]
                site.revision += 1
                site.save(update_fields=['headline','tagline','design_config','sections_config','pages_config','revision'])
                self.assertEqual(self.upload(kind, alt='Fictional preview artwork').status_code,302)
                site.refresh_from_db(); before = (site.revision,site.hero_asset_id,site.current_release_id,site.events.count())
                with patch('publishing.ai_provider._send_request') as text_ai, patch('publishing.image_ai_provider._send_request') as image_ai:
                    page = self.client.get(self.review(kind))
                text_ai.assert_not_called(); image_ai.assert_not_called()
                self.assertEqual(page.status_code,200)
                self.assertContains(page,'&lt;script&gt;Fictional new headline&lt;/script&gt;',html=False)
                self.assertNotContains(page,'<script>Fictional new headline</script>',html=False)
                labels = {str(c['label']) for c in page.context['changes']}
                self.assertTrue({'Headline','Tagline','Visual design','Hero image','Page modules','Independent pages'} <= labels)
                modules = next(c for c in page.context['changes'] if str(c['label']) == 'Page modules')
                self.assertEqual((modules['before'],modules['after']),(2,2))
                self.assertContains(page,'Fictional preview artwork')
                self.assertContains(page,reverse('private_journal_image' if kind=='journal' else 'private_portal_image',args=[slug,site.hero_asset_id]))
                site.refresh_from_db()
                self.assertEqual((site.revision,site.hero_asset_id,site.current_release_id,site.events.count()),before)
                self.assertEqual(self.client.post(self.review(kind)).status_code,405)

    def test_selected_journal_list_compares_current_visible_state(self):
        self.login('portal')
        self.assertEqual(self.publish('portal').status_code,302)
        self.psite.refresh_from_db(); self.psite.journal_ids = [self.journal.pk]
        self.psite.save(update_fields=['journal_ids'])
        page = self.client.get(self.review('portal'))
        self.assertContains(page,'Selected journals')
        self.assertContains(page,'Fictional journal')
        self.assertContains(page,'not linked')
        change = next(c for c in page.context['changes'] if str(c['label']) == 'Selected journals')
        self.assertEqual(change['before'],[])
        self.assertEqual(change['after'][0]['name'],'Fictional journal')

    def test_selected_journal_name_change_is_visible_without_a_portal_edit(self):
        self.psite.journal_ids = [self.journal.pk]
        self.psite.save(update_fields=['journal_ids'])
        self.assertEqual(self.publish('portal').status_code,302)
        self.journal.name = 'Renamed fictional journal'
        self.journal.save(update_fields=['name'])
        page = self.client.get(self.review('portal'))
        self.assertEqual(page.status_code,200)
        change = next(c for c in page.context['changes'] if str(c['label']) == 'Selected journals')
        self.assertEqual(change['before'][0]['name'],'Fictional journal')
        self.assertEqual(change['after'][0]['name'],'Renamed fictional journal')

    def test_missing_or_corrupt_hero_is_reported_without_exposing_bytes(self):
        self.assertEqual(self.publish('journal').status_code,302)
        self.assertEqual(self.upload('journal').status_code,302)
        self.jsite.refresh_from_db(); asset = self.jsite.hero_asset
        asset.image_data = b'corrupted'; asset.save(update_fields=['image_data'])
        page = self.client.get(self.review('journal'))
        self.assertContains(page,'Image unavailable')
        self.assertIsNone(page.context['draft_hero']['image_url'])

    def test_access_revocation_cross_site_and_nonlocal_modes(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                self.assertEqual(self.client.get(self.review(kind)).status_code,200)
                for user in (self.author,self.admin):
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(self.review(kind)).status_code,404)
                self.login(kind)
                with self.settings(DEBUG=False):
                    self.assertEqual(self.client.get(self.review(kind)).status_code,404)
                with self.settings(IPS_LOCAL_PUBLICATION_ENABLED=False):
                    self.assertEqual(self.client.get(self.review(kind)).status_code,404)
                if kind == 'journal':
                    Membership.objects.filter(journal=self.journal,user=self.journal_manager,role=Membership.Role.SITE_MANAGER).update(active=False)
                else:
                    OrganisationMembership.objects.filter(organisation=self.org,user=self.portal_manager,role=OrganisationMembership.Role.PORTAL_MANAGER).update(active=False)
                self.assertEqual(self.client.get(self.review(kind)).status_code,404)

    def test_release_from_another_site_cannot_be_compared(self):
        self.assertEqual(self.publish('journal').status_code,302)
        self.jsite.refresh_from_db()
        JournalSiteDraft.objects.create(journal=self.other_journal,brand_name='Other',headline='Other',
                 introduction='Other',created_by=self.other_journal_manager,current_release=self.jsite.current_release)
        self.client.force_login(self.other_journal_manager)
        self.assertEqual(self.client.get(self.review('journal',self.other_journal.slug)).status_code,404)
