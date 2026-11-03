from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from publishing.models import JournalSiteDraft, Membership, OrganisationMembership
from publishing.site_change_review_views import _block_side
from . import test_site_images as helpers


def block(heading, body, visible=True, kind='text', link_label='', link_url=''):
    return {'kind':kind, 'heading':heading, 'body':body, 'visible':visible,
            'link_label':link_label, 'link_url':link_url}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SiteBlockReviewTests(TestCase):
    setUpTestData = helpers.SiteImageTests.__dict__['setUpTestData']
    owner = helpers.SiteImageTests.owner
    route = helpers.SiteImageTests.route
    login = helpers.SiteImageTests.login
    token = helpers.SiteImageTests.token
    publish = helpers.SiteImageTests.publish

    def review(self, kind, page_slug='about'):
        route = 'journal_site_block_review' if kind == 'journal' else 'publisher_site_block_review'
        return reverse(route, args=[self.owner(kind)[1],page_slug])

    def test_page_block_review_is_scoped_read_only_and_escaped(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                site = self.owner(kind)[2]
                site.pages_config = [{'slug':'about','title':'About','body':'Intro','visible':True,
                    'blocks':[block('Old scope','Old <script>alert(1)</script> text'),
                              block('Old policy','Private note',False,'link','Read policy','https://example.org/policy')]}]
                site.save(update_fields=['pages_config'])
                self.assertEqual(self.publish(kind).status_code,302)
                site.refresh_from_db()
                site.pages_config[0]['blocks'] = [block('New policy','Now visible',True,'link','Read policy','https://example.org/policy'),
                                                   block('New scope','Updated text')]
                site.pages_config[0]['visible'] = False
                site.save(update_fields=['pages_config'])
                before = (site.revision,site.current_release_id,site.events.count())
                change_route = 'journal_site_change_review' if kind == 'journal' else 'publisher_site_change_review'
                summary = self.client.get(reverse(change_route,args=[self.owner(kind)[1]]))
                self.assertContains(summary,self.review(kind))
                with patch('publishing.ai_provider._send_request') as text_ai, patch('publishing.image_ai_provider._send_request') as image_ai:
                    response = self.client.get(self.review(kind))
                text_ai.assert_not_called(); image_ai.assert_not_called()
                self.assertEqual(response.status_code,200)
                self.assertIn('no-store',response['Cache-Control'])
                self.assertEqual(response['X-Robots-Tag'],'noindex, nofollow, noarchive')
                self.assertEqual([b['heading'] for b in response.context['before']['blocks']],['Old scope','Old policy'])
                self.assertEqual([b['heading'] for b in response.context['after']['blocks']],['New policy','New scope'])
                self.assertFalse(response.context['before']['blocks'][1]['visible'])
                self.assertContains(response,'&lt;script&gt;alert(1)&lt;/script&gt;',html=False)
                self.assertNotContains(response,'<script>alert(1)</script>',html=False)
                self.assertContains(response,'https://example.org/policy')
                self.assertNotContains(response,'href="https://example.org/policy"',html=False)
                self.assertContains(response,'not automatically matched')
                site.refresh_from_db()
                self.assertEqual((site.revision,site.current_release_id,site.events.count()),before)
                self.assertEqual(self.client.post(self.review(kind)).status_code,405)
                self.client.force_login(self.author)
                self.assertEqual(self.client.get(self.review(kind)).status_code,404)
                self.client.force_login(self.admin)
                self.assertEqual(self.client.get(self.review(kind)).status_code,404)
                self.client.force_login(self.other_journal_manager if kind == 'journal' else self.other_portal_manager)
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

    def test_unpublished_absent_and_invalid_sides_are_distinct(self):
        site = self.jsite
        site.pages_config = [{'slug':'about','title':'About','body':'','visible':True,'blocks':[block('One','Text')]}]
        site.save(update_fields=['pages_config']); self.login('journal')
        response = self.client.get(self.review('journal'))
        self.assertEqual(response.context['before']['state'],'unpublished')
        self.assertContains(response,'No published test version yet')
        self.assertEqual(self.publish('journal').status_code,302)
        site.refresh_from_db(); site.pages_config = []; site.save(update_fields=['pages_config'])
        response = self.client.get(self.review('journal'))
        self.assertEqual(response.context['after']['state'],'absent')
        self.assertContains(response,'Page not present in this version')
        site.pages_config = [{'slug':'','title':'INVALID-PRIVATE-TEXT','body':'','visible':True}]
        site.save(update_fields=['pages_config'])
        response = self.client.get(self.review('journal'))
        self.assertEqual(response.context['after']['state'],'unavailable')
        self.assertContains(response,'Page details unavailable')
        self.assertNotContains(response,'INVALID-PRIVATE-TEXT')
        self.assertEqual(self.client.get(self.review('journal','missing')).status_code,404)

    def test_block_metadata_and_grid_are_validated_before_display(self):
        item = block('Image','Caption')
        item.update(image_asset_id='8cba7e40-9fe1-4328-8d97-1fd9a35df943',image_alt='Fictional image',
                    image_fit='cover',image_position='center',grid_span=6)
        pages = [{'slug':'about','title':'About','body':'','visible':True,'blocks':[item],
                  'block_layout':{'gap':24,'alignment':'stretch'}}]
        side = _block_side(pages,'about')
        self.assertEqual(side['blocks'][0]['image_alt'],'Fictional image')
        self.assertEqual(str(side['blocks'][0]['image_fit']),'Crop to fill')
        self.assertEqual(str(side['blocks'][0]['image_position']),'Centre')
        self.assertEqual(side['blocks'][0]['grid_span'],6)
        pages[0]['blocks'][0]['grid_span'] = 13
        self.assertEqual(_block_side(pages,'about')['state'],'unavailable')

    def test_release_from_another_site_cannot_be_compared(self):
        self.jsite.pages_config = [{'slug':'about','title':'About','body':'','visible':True,'blocks':[block('One','Text')]}]
        self.jsite.save(update_fields=['pages_config'])
        self.assertEqual(self.publish('journal').status_code,302)
        self.jsite.refresh_from_db()
        JournalSiteDraft.objects.create(journal=self.other_journal,brand_name='Other',headline='Other',
            introduction='Other',created_by=self.other_journal_manager,current_release=self.jsite.current_release,
            pages_config=[{'slug':'about','title':'Other page','body':'','visible':True}])
        self.client.force_login(self.other_journal_manager)
        url = reverse('journal_site_block_review',args=[self.other_journal.slug,'about'])
        self.assertEqual(self.client.get(url).status_code,404)
