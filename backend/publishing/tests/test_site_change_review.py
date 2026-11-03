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

    def test_page_details_and_module_order_for_both_site_types(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                site = self.owner(kind)[2]
                defaults = journal_sections(site) if kind == 'journal' else portal_sections(site)
                site.sections_config = defaults + [{'kind':'custom','heading':'Fictional resources','body':'Old','visible':False}]
                site.pages_config = [
                    {'slug':'authors','title':'For authors','body':'Old instructions','visible':True},
                    {'slug':'board','title':'Editorial board','body':'Old board','visible':True},
                    {'slug':'old-page','title':'Old page','body':'Archive','visible':False},
                ]
                site.save(update_fields=['sections_config','pages_config'])
                self.assertEqual(self.publish(kind).status_code,302)
                site.refresh_from_db()
                site.sections_config = [site.sections_config[2], *site.sections_config[:2]]
                site.sections_config[0]['visible'] = True
                site.pages_config = [
                    {'slug':'board','title':'<b>New board</b>','body':'New board','visible':False},
                    {'slug':'authors','title':'For authors','body':'Old instructions','visible':True},
                    {'slug':'new-page','title':'New page','body':'Welcome','visible':True},
                ]
                site.save(update_fields=['sections_config','pages_config'])
                response = self.client.get(self.review(kind))
                self.assertEqual(response.status_code,200)
                changes = {str(c['label']):c for c in response.context['changes']}
                modules = changes['Page modules']
                self.assertEqual(modules['before_items'][2]['title'],'Fictional resources')
                self.assertFalse(modules['before_items'][2]['visible'])
                self.assertEqual(modules['after_items'][0]['title'],'Fictional resources')
                self.assertTrue(modules['after_items'][0]['visible'])
                pages = changes['Independent pages']['page_changes']
                self.assertEqual([str(p['status']) for p in pages],['Changed','Changed','Added','Removed'])
                self.assertEqual([str(f) for f in pages[0]['fields']],['Title','Page text','Visibility','Position'])
                self.assertEqual(''.join(p['text'] for p in pages[0]['text_comparison']['before']),'Old board')
                self.assertEqual(''.join(p['text'] for p in pages[0]['text_comparison']['after']),'New board')
                self.assertIsNone(pages[1]['text_comparison'])
                self.assertEqual((pages[0]['before']['position'],pages[0]['after']['position']),(2,1))
                self.assertEqual(pages[2]['after']['slug'],'new-page')
                self.assertEqual(pages[3]['before']['slug'],'old-page')
                self.assertContains(response,'&lt;b&gt;New board&lt;/b&gt;',html=False)
                self.assertNotContains(response,'<b>New board</b>',html=False)
                self.assertContains(response,'A renamed URL appears as a removed page and an added page')

    def test_page_text_comparison_escapes_content_and_remains_private(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                site = self.owner(kind)[2]
                site.pages_config = [{'slug':'example','title':'Example','body':'Old <script>alert(1)</script> text','visible':False}]
                site.save(update_fields=['pages_config'])
                self.assertEqual(self.publish(kind).status_code,302)
                site.refresh_from_db()
                site.pages_config[0]['body'] = 'New <script>alert(1)</script> text'
                site.save(update_fields=['pages_config'])
                before = (site.revision,site.current_release_id,site.events.count())
                response = self.client.get(self.review(kind))
                self.assertContains(response,'Compare page text')
                self.assertContains(response,'<del>Old</del>',html=False)
                self.assertContains(response,'<ins>New</ins>',html=False)
                self.assertContains(response,'&lt;script&gt;alert(1)&lt;/script&gt;',html=False)
                self.assertNotContains(response,'<script>alert(1)</script>',html=False)
                site.refresh_from_db()
                self.assertEqual((site.revision,site.current_release_id,site.events.count()),before)
                self.client.force_login(self.author)
                self.assertEqual(self.client.get(self.review(kind)).status_code,404)

    def test_added_and_removed_page_text_distinguishes_absent_from_empty(self):
        site = self.jsite
        site.pages_config = [{'slug':'old','title':'Old','body':'Old text','visible':True},
                             {'slug':'empty','title':'Empty','body':'','visible':True}]
        site.save(update_fields=['pages_config'])
        self.assertEqual(self.publish('journal').status_code,302)
        site.refresh_from_db()
        site.pages_config = [{'slug':'empty','title':'Empty','body':'Now there is text','visible':True},
                             {'slug':'new','title':'New','body':'New text','visible':True}]
        site.save(update_fields=['pages_config'])
        response = self.client.get(self.review('journal'))
        pages = next(c for c in response.context['changes'] if str(c['label']) == 'Independent pages')['page_changes']
        self.assertTrue(pages[0]['text_comparison']['before_present'])
        self.assertTrue(pages[0]['text_comparison']['before_empty'])
        self.assertFalse(pages[1]['text_comparison']['before_present'])
        self.assertFalse(pages[2]['text_comparison']['after_present'])
        self.assertContains(response,'No page text')

    def test_invalid_stored_page_and_module_details_are_not_rendered(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.assertEqual(self.publish(kind).status_code,302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                site.sections_config = [{'kind':'custom','heading':'UNTRUSTED-MODULE','body':'x','visible':'yes'}]
                site.pages_config = [{'slug':'','title':'UNTRUSTED-PAGE','body':'x','visible':True}]
                site.save(update_fields=['sections_config','pages_config'])
                response = self.client.get(self.review(kind))
                self.assertEqual(response.status_code,200)
                self.assertContains(response,'Module details unavailable')
                self.assertContains(response,'Page details unavailable because one version is invalid.')
                self.assertNotContains(response,'UNTRUSTED-MODULE')
                self.assertNotContains(response,'UNTRUSTED-PAGE')

    def test_legacy_module_order_change_is_reported_when_lists_remain_empty(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.assertEqual(self.publish(kind).status_code,302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(site.sections_config,[])
                if kind == 'journal':
                    site.layout = 'magazine'
                    site.save(update_fields=['layout'])
                else:
                    site.design_config = {**site.design_config,'section_order':'archive_first'}
                    site.save(update_fields=['design_config'])
                response = self.client.get(self.review(kind))
                change = next(c for c in response.context['changes'] if str(c['label']) == 'Page modules')
                self.assertEqual((change['before'],change['after']),(2,2))
                self.assertNotEqual(change['before_items'][0]['type'],change['after_items'][0]['type'])

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
