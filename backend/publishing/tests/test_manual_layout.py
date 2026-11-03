from copy import deepcopy
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (JournalSiteEvent, Membership, OrganisationMembership, OrganisationPortalEvent)
from . import test_page_blocks as helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ManualLayoutTests(TestCase):
    setUpTestData = helpers.PageBlockTests.__dict__['setUpTestData']
    owner = helpers.PageBlockTests.owner
    route = helpers.PageBlockTests.route
    login = helpers.PageBlockTests.login
    token = helpers.PageBlockTests.token
    upload = helpers.PageBlockTests.upload
    publish = helpers.PageBlockTests.publish
    add_image = helpers.PageBlockTests.add_image
    page_url = helpers.PageBlockTests.page_url
    pages_token = helpers.PageBlockTests.pages_token
    payload = helpers.PageBlockTests.payload
    save_pages = helpers.PageBlockTests.save_pages
    page = staticmethod(helpers.PageBlockTests.page)
    block = staticmethod(helpers.PageBlockTests.block)
    block_url = helpers.PageBlockTests.block_url
    block_payload = helpers.PageBlockTests.block_payload
    save_blocks = helpers.PageBlockTests.save_blocks

    def url(self, kind, action='', slug='author-guidelines'):
        prefix='journal' if kind=='journal' else 'publisher'
        return reverse(prefix+'_manual_layout'+('_'+action if action else ''), args=[self.owner(kind)[1],slug])

    def seed(self, kind):
        asset=self.add_image(kind,'Fictional image')
        self.assertEqual(self.save_pages(kind,[self.page()]).status_code,302)
        blocks=[self.block('First',asset,True,url='https://example.org/info'),
                self.block('Hidden secret',asset,False), self.block('Last',kind='text')]
        blocks[0]['grid_span']=8
        blocks[2]['grid_span']=4
        self.assertEqual(self.save_blocks(kind,blocks).status_code,302)
        return asset

    def data(self, kind):
        self.login(kind)
        response=self.client.get(self.url(kind))
        self.assertEqual(response.status_code,200)
        return {'revision':str(response.context['site'].revision),'fingerprint':response.context['fingerprint'],
                'settings-gap':'18','settings-alignment':'stretch',
                'blocks-TOTAL_FORMS':'3','blocks-INITIAL_FORMS':'3','blocks-MIN_NUM_FORMS':'0','blocks-MAX_NUM_FORMS':'20',
                'blocks-0-block_id':'b0','blocks-0-order':'2','blocks-0-grid_span':'8',
                'blocks-1-block_id':'b1','blocks-1-order':'3','blocks-1-grid_span':'4',
                'blocks-2-block_id':'b2','blocks-2-order':'1','blocks-2-grid_span':'4'}

    def test_preview_return_apply_preserves_content_and_published_snapshot(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                asset=self.seed(kind)
                self.assertEqual(self.publish(kind).status_code,302)
                site=self.owner(kind)[2];site.refresh_from_db();original=deepcopy(site.pages_config)
                data=self.data(kind)
                with patch('publishing.ai_provider._send_request') as ai:
                    preview=self.client.post(self.url(kind,'preview'),data)
                ai.assert_not_called();self.assertEqual(len(mail.outbox),0)
                self.assertEqual(preview.status_code,200)
                self.assertContains(preview,'Private layout preview')
                self.assertNotContains(preview,'Private AI layout preview')
                self.assertContains(preview,'Last')
                self.assertNotContains(preview,'Hidden secret')
                self.assertContains(preview,'--block-grid-span:8')
                self.assertEqual([block['heading'] for block in preview.context['page_blocks']], ['Last','First'])
                self.assertIn('no-store',preview['Cache-Control'])
                site.refresh_from_db();self.assertEqual(site.pages_config,original)
                token=preview.context['layout_reviewed']
                back=self.client.post(self.url(kind,'edit'),{'revision':data['revision'],'reviewed_layout':token})
                self.assertEqual(back.status_code,200)
                self.assertEqual(back.context['settings_form'].initial['gap'],18)
                self.assertEqual(back.context['formset'].forms[0].initial['order'],2)
                self.assertEqual(back.context['formset'].forms[2].initial['order'],1)
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token}).status_code,400)
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}).status_code,302)
                site.refresh_from_db();new=site.pages_config[0]
                self.assertEqual(new['block_layout'],{'gap':18,'alignment':'stretch'})
                self.assertEqual([b['heading'] for b in new['blocks']],['Last','First','Hidden secret'])
                self.assertEqual(new['blocks'][1]['image_asset_id'],str(asset.pk))
                self.assertEqual(new['blocks'][1]['link_url'],'https://example.org/info')
                self.assertEqual(new['blocks'][2]['visible'],False)
                self.assertEqual(new['blocks'][2]['image_asset_id'],str(asset.pk))
                public=Client().get(self.page_url(kind,'public','author-guidelines'))
                self.assertNotContains(public,'--block-grid-span:8')
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}).status_code,409)
                model=JournalSiteEvent if kind=='journal' else OrganisationPortalEvent
                self.assertTrue(model.objects.filter(site=site,action__contains='manual_layout_applied').exists())

    def test_rejects_invalid_or_stale_inputs_and_preserves_draft(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.seed(kind);data=self.data(kind)
                site=self.owner(kind)[2];site.refresh_from_db();original=deepcopy(site.pages_config)
                for key,value,code in [('blocks-0-grid_span','13',400),('blocks-0-order','1',400),
                                       ('blocks-1-block_id','b0',400),('settings-gap','65',400),
                                       ('blocks-TOTAL_FORMS','2',409),('fingerprint','tampered',409)]:
                    self.assertEqual(self.client.post(self.url(kind,'preview'),{**data,key:value}).status_code,code)
                preview=self.client.post(self.url(kind,'preview'),data);token=preview.context['layout_reviewed']
                self.assertEqual(self.client.post(self.url(kind,'edit'),{'revision':data['revision'],'reviewed_layout':token+'X'}).status_code,400)
                site.refresh_from_db();site.headline='Changed elsewhere';site.revision+=1;site.save(update_fields=['headline','revision'])
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}).status_code,409)
                self.assertEqual(self.client.post(self.url(kind,'preview'),data).status_code,409)
                site.refresh_from_db();self.assertEqual(site.pages_config,original)

    def test_roles_csrf_and_token_bound_to_requester(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.seed(kind);data=self.data(kind)
                csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(self.url(kind,'preview'),data).status_code,403)
                preview=self.client.post(self.url(kind,'preview'),data);token=preview.context['layout_reviewed']
                other=self.other_journal_manager if kind=='journal' else self.other_portal_manager
                if kind=='journal':
                    Membership.objects.create(journal=self.journal,user=other,role=Membership.Role.SITE_MANAGER)
                else:
                    OrganisationMembership.objects.create(organisation=self.org,user=other,role=OrganisationMembership.Role.PORTAL_MANAGER)
                self.client.force_login(other)
                self.assertEqual(self.client.get(self.url(kind)).status_code,200)
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}).status_code,400)
                for user in (self.author,self.admin):
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(self.url(kind)).status_code,404)
                self.login(kind)
                if kind=='journal': Membership.objects.filter(journal=self.journal,user=self.journal_manager,role=Membership.Role.SITE_MANAGER).update(active=False)
                else: OrganisationMembership.objects.filter(organisation=self.org,user=self.portal_manager,role=OrganisationMembership.Role.PORTAL_MANAGER).update(active=False)
                self.assertEqual(self.client.post(self.url(kind,'apply'),{'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}).status_code,404)

    def test_canvas_previews_escaped_saved_content_and_private_images(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                asset=self.add_image(kind,'Private image description')
                self.assertEqual(self.save_pages(kind,[self.page()]).status_code,302)
                visible=self.block('Visible title',asset,True,url='https://example.org/somewhere')
                visible['body']='Text with <script>alert(1)</script> stays text.'
                visible['design']={'layout':'image_left','width':100,'background':'#e2f1e7','accent':'#234f40',
                                   'alignment':'left','padding':20,'radius':12,'gap':16}
                hidden=self.block('Hidden block',asset,False)
                hidden['body']='HIDDEN_BODY_NOT_IN_CANVAS'
                self.assertEqual(self.save_blocks(kind,[visible,hidden]).status_code,302)
                self.login(kind)
                response=self.client.get(self.url(kind))
                self.assertContains(response,'data-layout-content-mode="yes"')
                self.assertContains(response,'data-layout-content')
                self.assertContains(response,'Text with &lt;script&gt;alert(1)&lt;/script&gt; stays text.')
                self.assertNotContains(response,'<script>alert(1)</script>')
                self.assertContains(response,'--block-bg:#e2f1e7')
                self.assertContains(response,'block-layout-image_left')
                self.assertContains(response,self.route(kind,'private',asset.pk))
                self.assertNotContains(response,'HIDDEN_BODY_NOT_IN_CANVAS')
                self.assertNotContains(response,'https://example.org/somewhere')
                self.assertEqual(self.client.get(self.route(kind,'private',asset.pk)).status_code,200)
                self.assertNotEqual(Client().get(self.route(kind,'private',asset.pk)).status_code,200)
                self.assertIn('no-store',response['Cache-Control'])

    def test_expired_and_cross_page_tokens_do_not_apply(self):
        self.seed('journal')
        self.assertEqual(self.save_pages('journal',[self.page(),self.page(slug='editorial-board')]).status_code,302)
        data=self.data('journal')
        preview=self.client.post(self.url('journal','preview'),data)
        token=preview.context['layout_reviewed']
        payload={'revision':data['revision'],'reviewed_layout':token,'confirm':'yes'}
        with patch('publishing.manual_layout_views.MAX_TOKEN_AGE',-1):
            self.assertEqual(self.client.post(self.url('journal','apply'),payload).status_code,400)
        self.assertEqual(self.client.post(self.url('journal','apply','editorial-board'),payload).status_code,400)
        site=self.owner('journal')[2];site.refresh_from_db()
        self.assertEqual([block['heading'] for block in site.pages_config[0]['blocks']],['First','Hidden secret','Last'])

    def test_empty_pages_and_chinese_local_only(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind,[self.page()]).status_code,302)
                self.login(kind)
                page=self.client.get(self.url(kind))
                self.assertContains(page,'Start with a content block')
                self.assertContains(self.client.get(self.url(kind),HTTP_ACCEPT_LANGUAGE='zh-hans'),'可视化布局设计器')
                data={'revision':str(page.context['site'].revision),'fingerprint':page.context['fingerprint']}
                self.assertEqual(self.client.post(self.url(kind,'preview'),data).status_code,409)
                with self.settings(DEBUG=False):self.assertEqual(self.client.get(self.url(kind)).status_code,404)
                with self.settings(IPS_LOCAL_PUBLICATION_ENABLED=False):self.assertEqual(self.client.get(self.url(kind)).status_code,404)
