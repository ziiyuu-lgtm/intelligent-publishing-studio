from copy import deepcopy
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.core import signing

from publishing.models import (Membership, OrganisationMembership, JournalSiteDraft,
                               OrganisationPortalDraft, SiteImageAsset)
from publishing.site_ai_provider import LEGACY_PROMPT_VERSION
from publishing.site_design import sample_by_id
from publishing.site_images import normalise_image
from publishing.visual_ai_review_views import SALT, _decode, _option_fingerprint
from publishing import site_views, portal_views, site_ai_views, portal_ai_views
from . import test_site_ai as site_helpers
from . import test_publisher_portal_ai as portal_helpers
from . import test_site_images as image_helpers


class ReviewChecks:
    kind = None

    def url(self, action, run, index=0):
        return reverse(self.kind + '_visual_ai_' + action, args=[self.owner_slug(), run.pk, index])

    def form_data(self, run):
        option = deepcopy(run.options[0])
        option.update(primary='#a13d66', surface='#f7e8ee', background='#fffafa',
                      composition='split', alignment='center', revision=str(run.site_revision))
        option['show_feature'] = 'on'
        return option

    def image_asset(self, site, alt='Fictional cover'):
        owner = {'journal_site':site} if isinstance(site, JournalSiteDraft) else {'portal_site':site}
        return SiteImageAsset.objects.create(**owner, **normalise_image(image_helpers.uploaded()),
                                             default_alt=alt, uploaded_by=self.manager)

    def foreign_image(self):
        if self.kind == 'journal':
            other = OrganisationPortalDraft.objects.create(organisation=self.journal.organisation,
                created_by=self.manager, brand_name='Other', headline='Other', introduction='Other',
                design_config=sample_by_id('heritage')['config'])
        else:
            other = JournalSiteDraft.objects.create(journal=self.journal, created_by=self.manager,
                brand_name='Other', headline='Other', introduction='Other')
        return self.image_asset(other, alt='FOREIGN IMAGE')

    def test_hero_choice_is_previewed_and_applied_with_the_reviewed_design(self):
        asset = self.image_asset(self.site)
        run = self.completed(); self.transport.reset_mock()
        editor = self.client.get(self.url('customise', run))
        self.assertContains(editor, 'Choose a hero image')
        self.assertContains(editor, str(asset.pk))
        data = {**self.form_data(run), 'hero_asset_id':str(asset.pk), 'hero_alt':'Fictional research illustration',
                'hero_fit':'contain', 'hero_position':'top'}
        preview = self.client.post(self.url('preview', run), data)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Fictional research illustration')
        self.assertContains(preview, 'object-fit:contain;object-position:top')
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id)
        token = preview.context['visual_reviewed']
        returned = self.client.post(self.url('edit',run), {'revision':str(run.site_revision),'reviewed_design':token})
        self.assertEqual(returned.context['hero_form'].initial['hero_asset_id'], str(asset.pk))
        self.assertEqual(returned.context['hero_form'].initial['hero_position'], 'top')
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':token,'confirm':'yes'}).status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.hero_asset_id, asset.pk)
        self.assertEqual((self.site.hero_alt,self.site.hero_fit,self.site.hero_position),
                         ('Fictional research illustration','contain','top'))
        self.assertEqual(run.applied_hero_content['sha256'], asset.sha256)
        self.assertEqual(run.applied_hero_content['asset_id'], str(asset.pk))
        self.assertEqual(self.site.revision, run.site_revision + 1)
        self.assertIsNone(self.site.current_release_id)
        self.transport.assert_not_called()

    def test_foreign_invalid_and_deleted_images_cannot_be_applied(self):
        asset = self.image_asset(self.site)
        foreign = self.foreign_image()
        run = self.completed(); self.transport.reset_mock()
        editor = self.client.get(self.url('customise',run))
        self.assertNotContains(editor, 'FOREIGN IMAGE')
        good = {**self.form_data(run), 'hero_asset_id':str(asset.pk), 'hero_alt':'Fictional cover',
                'hero_fit':'cover', 'hero_position':'center'}
        for changed in ({'hero_asset_id':str(foreign.pk)}, {'hero_alt':''}, {'hero_fit':'bad;css'}):
            self.assertEqual(self.client.post(self.url('preview',run), {**good,**changed}).status_code, 400)
        preview = self.client.post(self.url('preview',run), good)
        token = preview.context['visual_reviewed']
        replacement = normalise_image(image_helpers.uploaded(image_helpers.image_bytes(size=(20,20))))
        for key, value in replacement.items(): setattr(asset, key, value)
        asset.save(update_fields=list(replacement))
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':token,'confirm':'yes'}).status_code, 400)
        asset.delete()
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':token,'confirm':'yes'}).status_code, 400)
        self.site.refresh_from_db();run.refresh_from_db()
        self.assertIsNone(self.site.hero_asset_id)
        self.assertIsNone(run.applied_revision)
        self.transport.assert_not_called()

    def test_existing_hero_can_be_removed_without_touching_the_published_version(self):
        asset = self.image_asset(self.site)
        self.site.hero_asset = asset; self.site.hero_alt = 'Original image'
        self.site.save(update_fields=['hero_asset','hero_alt'])
        run = self.completed()
        data = {**self.form_data(run),'hero_asset_id':'','hero_alt':'ignored',
                'hero_fit':'cover','hero_position':'center'}
        preview = self.client.post(self.url('preview',run), data)
        self.assertEqual(preview.status_code,200)
        self.assertFalse(preview.context['hero_image_url'])
        self.site.refresh_from_db();self.assertEqual(self.site.hero_asset_id,asset.pk)
        token = preview.context['visual_reviewed']
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':token,'confirm':'yes'}).status_code,302)
        self.site.refresh_from_db();self.assertIsNone(self.site.hero_asset_id)

    def test_old_preview_submission_keeps_the_existing_hero(self):
        asset = self.image_asset(self.site)
        self.site.hero_asset = asset; self.site.hero_alt = 'Existing image'
        self.site.save(update_fields=['hero_asset','hero_alt'])
        run = self.completed()
        preview = self.client.post(self.url('preview',run), self.form_data(run))
        self.assertContains(preview, 'Existing image')
        token = preview.context['visual_reviewed']
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':token,'confirm':'yes'}).status_code,302)
        self.site.refresh_from_db();self.assertEqual(self.site.hero_asset_id,asset.pk)

    def test_pre_upgrade_signed_preview_keeps_the_existing_hero(self):
        asset = self.image_asset(self.site)
        self.site.hero_asset = asset; self.site.hero_alt = 'Existing image'
        self.site.save(update_fields=['hero_asset','hero_alt'])
        run = self.completed()
        self.site.refresh_from_db()
        fingerprint = (site_views._fingerprint(self.site) if self.kind == 'journal'
                       else portal_views._fingerprint(self.site))
        option = (site_ai_views._options(run) if self.kind == 'journal' else portal_ai_views._options(run))[0]
        token_kind = 'journal' if self.kind == 'journal' else 'portal'
        old_token = signing.dumps({'user':str(self.manager.pk),'site':str(self.site.pk),
            'run':str(run.pk),'index':0,'kind':token_kind,'revision':self.site.revision,
            'fingerprint':fingerprint,'source':_option_fingerprint(option),'design':option},
            salt=SALT,compress=True)
        _decode(old_token,self.manager,self.site,run,0,token_kind,fingerprint,option)
        self.assertEqual(self.client.post(self.url('apply',run), {'revision':str(run.site_revision),
            'reviewed_design':old_token,'confirm':'yes'}).status_code,302)
        self.site.refresh_from_db();self.assertEqual(self.site.hero_asset_id,asset.pk)

    def test_customise_preview_return_and_apply_are_separate(self):
        run = self.completed()
        self.transport.reset_mock()
        site = self.site
        site.refresh_from_db()
        before = deepcopy(site.design_config)
        result = self.client.get(self.result_url_for(run))
        self.assertContains(result, 'Customise before applying')
        edit = self.client.get(self.url('customise', run))
        self.assertEqual(edit.status_code, 200)
        self.assertContains(edit, 'Customise AI design')
        self.assertIn('no-store', edit['Cache-Control'])
        preview = self.client.post(self.url('preview', run), self.form_data(run))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Private customised design preview')
        self.assertContains(preview, 'Responsive website preview')
        self.assertContains(preview, 'sandbox="allow-same-origin"')
        self.assertContains(preview, 'visual_ai_devices.js')
        self.assertContains(preview, '--site-accent:#a13d66')
        self.assertContains(preview, 'composition-split')
        self.assertIn('no-store', preview['Cache-Control'])
        site.refresh_from_db(); self.assertEqual(site.design_config, before)
        token = preview.context['visual_reviewed']
        returned = self.client.post(self.url('edit', run), {'revision':str(run.site_revision),'reviewed_design':token})
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.context['form'].initial['primary'], '#a13d66')
        self.assertEqual(returned.context['form'].initial['composition'], 'split')
        data = {'revision':str(run.site_revision),'reviewed_design':token}
        self.assertEqual(self.client.post(self.url('apply',run), data).status_code, 400)
        self.assertEqual(self.client.post(self.url('apply',run), {**data,'confirm':'yes'}).status_code, 302)
        site.refresh_from_db();run.refresh_from_db()
        self.assertEqual(site.design_config['primary'], '#a13d66')
        self.assertEqual(site.design_config['composition'], 'split')
        self.assertEqual(run.applied_option_content, site.design_config)
        self.assertIsNone(site.current_release_id)
        self.assertEqual(self.client.post(self.url('apply',run), {**data,'confirm':'yes'}).status_code, 409)
        self.transport.assert_not_called()

    def test_invalid_tampered_stale_and_unauthorised_requests_fail(self):
        run = self.completed(); self.transport.reset_mock()
        data = self.form_data(run)
        invalid = self.client.post(self.url('preview',run), {**data,'primary':'<script>'})
        self.assertEqual(invalid.status_code, 400)
        self.assertNotContains(invalid, '<script>', status_code=400)
        preview = self.client.post(self.url('preview',run), data)
        token = preview.context['visual_reviewed']
        payload = {'revision':str(run.site_revision),'reviewed_design':token,'confirm':'yes'}
        self.assertEqual(self.client.post(self.url('apply',run), {**payload,'reviewed_design':token+'x'}).status_code,400)
        self.assertEqual(self.client.post(self.url('apply',run,1), payload).status_code,400)
        original = deepcopy(run.options)
        run.options[0]['primary'] = '#abcdef'; run.save(update_fields=['options'])
        self.assertEqual(self.client.post(self.url('apply',run), payload).status_code,400)
        run.options = original; run.save(update_fields=['options'])
        with patch('publishing.visual_ai_review_views.MAX_TOKEN_AGE',-1):
            self.assertEqual(self.client.post(self.url('apply',run), payload).status_code,400)
        csrf = Client(enforce_csrf_checks=True);csrf.force_login(self.manager)
        self.assertEqual(csrf.post(self.url('apply',run),payload).status_code,403)
        self.client.force_login(self.other_manager_user())
        self.assertEqual(self.client.get(self.url('customise',run)).status_code,404)
        self.client.force_login(self.manager)
        if self.kind == 'journal':
            membership = Membership.objects.get(journal=self.journal,user=self.manager,role=Membership.Role.SITE_MANAGER)
        else:
            membership = OrganisationMembership.objects.get(organisation=self.org,user=self.manager,role=OrganisationMembership.Role.PORTAL_MANAGER)
        membership.active=False;membership.save(update_fields=['active'])
        self.assertEqual(self.client.post(self.url('apply',run),payload).status_code,404)
        membership.active=True;membership.save(update_fields=['active'])
        site=self.site;site.refresh_from_db();site.headline='Changed elsewhere';site.revision+=1
        site.save(update_fields=['headline','revision'])
        self.assertEqual(self.client.post(self.url('apply',run),payload).status_code,409)
        self.assertEqual(self.client.post(self.url('preview',run),data).status_code,409)
        self.transport.assert_not_called()


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL='test-model', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JournalVisualReviewTests(ReviewChecks, TestCase):
    kind='journal'
    setUpTestData=site_helpers.SiteAITests.__dict__['setUpTestData']
    setUp=site_helpers.SiteAITests.setUp
    data=site_helpers.SiteAITests.data
    request_design=site_helpers.SiteAITests.request_design
    completed=site_helpers.SiteAITests.completed
    def owner_slug(self): return self.journal.slug
    def other_manager_user(self): return self.second_manager
    def result_url_for(self,run): return reverse('site_ai_result',args=[self.journal.slug,run.pk])

    def test_legacy_suggestion_can_be_customised_and_reviewed(self):
        run = self.completed()
        run.prompt_version = LEGACY_PROMPT_VERSION
        run.options = [{'theme': 'forest', 'layout': 'classic'}]
        run.save(update_fields=['prompt_version', 'options'])
        self.transport.reset_mock()
        editor = self.client.get(self.url('customise', run))
        self.assertEqual(editor.status_code, 200)
        data = dict(editor.context['form'].initial)
        data.update(primary='#a13d66', revision=str(run.site_revision), show_feature='on')
        preview = self.client.post(self.url('preview', run), data)
        self.assertContains(preview, '--site-accent:#a13d66')
        token = preview.context['visual_reviewed']
        self.assertEqual(self.client.post(self.url('apply', run), {
            'revision': str(run.site_revision), 'reviewed_design': token, 'confirm': 'yes',
        }).status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.design_config['primary'], '#a13d66')
        self.assertEqual(run.applied_option_content, self.site.design_config)
        self.assertIsNone(self.site.current_release_id)
        self.transport.assert_not_called()


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL='test-model', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PublisherVisualReviewTests(ReviewChecks, TestCase):
    kind='publisher'
    setUpTestData=portal_helpers.PublisherPortalAITests.__dict__['setUpTestData']
    setUp=portal_helpers.PublisherPortalAITests.setUp
    data=portal_helpers.PublisherPortalAITests.data
    request_design=portal_helpers.PublisherPortalAITests.request_design
    completed=portal_helpers.PublisherPortalAITests.completed
    url_portal=portal_helpers.PublisherPortalAITests.url
    def owner_slug(self): return self.org.slug
    def other_manager_user(self): return self.second
    def result_url_for(self,run): return reverse('publisher_ai_result',args=[self.org.slug,run.pk])
    def url(self, action, *args, **kwargs):
        if action.startswith(('manage_', 'request_')):
            return self.url_portal(action,*args,**kwargs)
        return super().url(action,*args,**kwargs)
