from copy import deepcopy
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Membership, OrganisationMembership
from publishing.site_ai_provider import LEGACY_PROMPT_VERSION
from . import test_site_ai as site_helpers
from . import test_publisher_portal_ai as portal_helpers


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
