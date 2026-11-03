import base64
import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from publishing.ai_provider import ProviderError
from publishing.image_ai_provider import generate_image
from publishing.models import Membership, OrganisationMembership, SiteImageAsset, SiteImageGenerationRun
from publishing.site_images import normalise_image
from . import test_site_ai as site_helpers
from . import test_publisher_portal_ai as portal_helpers
from . import test_site_images as image_helpers


def response():
    return {'data':[{'b64_json':base64.b64encode(image_helpers.image_bytes(metadata=True)).decode('ascii')}]}


class ImageProviderTests(TestCase):
    def test_provider_request_contains_only_the_explicit_brief_and_settings(self):
        with patch('publishing.image_ai_provider._send_request', return_value=response()) as send:
            image = generate_image(api_key='test-key',model='gpt-image-2.5-flare',prompt='Fictional abstract art',
                                   size='1536x1024',safety_identifier='a'*64)
        req=send.call_args.args[0]; payload=json.loads(req.data)
        self.assertEqual(req.full_url,'https://api.openai.com/v1/images/generations')
        self.assertEqual(set(payload),{'model','prompt','size','n','quality','output_format','safety_identifier'})
        self.assertEqual(payload['prompt'],'Fictional abstract art')
        self.assertEqual(payload['n'],1)
        self.assertNotIn(b'UNIQUE_PRIVATE_METADATA',image['image_data'])
        self.assertEqual((image['width'],image['height']),(32,24))

    def test_provider_rejects_invalid_responses_and_inputs(self):
        bad=[{}, {'data':[]}, {'data':[{'b64_json':'!!!'}]},
             {'data':[{'b64_json':base64.b64encode(b'not an image').decode()}]},
             {'data':[{},{}]}]
        for value in bad:
            with self.subTest(value=value), patch('publishing.image_ai_provider._send_request',return_value=value):
                with self.assertRaises(ProviderError):
                    generate_image(api_key='test-key',model='model',prompt='Fictional',size='1024x1024',safety_identifier='a'*64)
        with patch('publishing.image_ai_provider._send_request') as send:
            with self.assertRaises(ProviderError):
                generate_image(api_key='test-key',model='model',prompt='Fictional',size='bad',safety_identifier='a'*64)
            send.assert_not_called()


class ImageWorkflowChecks:
    kind = None

    def slug(self): return self.journal.slug if self.kind == 'journal' else self.org.slug
    def route(self, action, run=None):
        args=[self.slug()]+([run.pk] if run else [])
        return reverse(self.kind + '_image_ai_' + action,args=args)
    def second_user(self): return self.second_manager if self.kind == 'journal' else self.second
    def policy_field(self): return 'site_image_enabled' if self.kind == 'journal' else 'portal_image_enabled'

    def payload(self):
        page=self.client.get(self.route('create'))
        self.assertEqual(page.status_code,200)
        return {'request_id':str(page.context['request_id']), 'revision':str(page.context['site'].revision),
            'fingerprint':page.context['fingerprint'],'policy_revision':str(page.context['policy_revision']),
            'model':page.context['model_name'],'brief':'A fictional abstract research landscape',
            'description':'A fictional landscape with circles','size':'1536x1024','confirm':'yes','synthetic':'yes'}

    def start(self, payload=None):
        with patch('publishing.image_ai_provider._send_request',return_value=response()) as send:
            result=self.client.post(self.route('create'),payload or self.payload())
        self.assertEqual(result.status_code,302)
        return SiteImageGenerationRun.objects.latest('created_at'),send

    def test_private_review_acceptance_and_library_lifecycle(self):
        data=self.payload();run,send=self.start(data)
        self.assertEqual(run.status,'completed');self.assertEqual(self.site.images.count(),0)
        sent=json.loads(send.call_args.args[0].data)
        self.assertEqual(sent['prompt'],data['brief'])
        self.assertNotIn(data['description'],json.dumps(sent))
        self.assertNotIn('SECRET',json.dumps(sent))
        self.assertNotIn(data['brief'],str(run.__dict__))
        self.assertContains(self.client.get(self.route('result',run)),'Review your image')
        history = reverse('journal_design_history' if self.kind == 'journal' else 'publisher_design_history', args=[self.slug()])
        self.assertContains(self.client.get(self.route('result',run)), history)
        self.assertContains(self.client.get(self.route('create')), history)
        private=self.client.get(self.route('image',run))
        self.assertEqual(private.status_code,200);self.assertIn('no-store',private['Cache-Control'])
        self.client.force_login(self.second_user())
        self.assertEqual(self.client.get(self.route('result',run)).status_code,404)
        self.assertEqual(self.client.get(self.route('image',run)).status_code,404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.route('accept',run),{}).status_code,400)
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,302)
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,302)
        run.refresh_from_db();self.site.refresh_from_db()
        self.assertEqual(self.site.images.count(),1)
        self.assertIsNotNone(run.accepted_at);self.assertIsNone(run.image_data)
        self.assertIsNone(self.site.hero_asset_id);self.assertEqual(self.site.revision,run.site_revision)
        self.assertEqual(self.client.get(self.route('image',run)).status_code,200)
        run.accepted_asset.delete();run.refresh_from_db()
        self.assertIsNone(run.accepted_asset_id);self.assertIsNotNone(run.accepted_at)
        self.assertContains(self.client.get(self.route('result',run)),'removed from the library')
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,302)
        self.assertEqual(self.site.images.count(),0)

    def page_examples(self):
        self.site.pages_config = [
            {'slug':'about', 'title':'About <research>', 'body':'A fictional introduction.', 'visible':True},
            {'slug':'editorial-board', 'title':'Editorial board', 'body':'Private draft content.', 'visible':False},
        ]
        self.site.save(update_fields=['pages_config'])

    def page_review(self, run, **options):
        response = self.client.get(self.route('page_preview', run), options)
        self.assertEqual(response.status_code, 200)
        return response.context['image_candidate_page']['candidate_reviewed']

    def test_candidate_page_preview_is_private_and_read_only(self):
        self.page_examples(); run, _ = self.start()
        before = self.site.pages_config
        url = self.route('page_preview', run)
        self.assertContains(self.client.get(self.route('result', run)), url)
        with patch('publishing.image_ai_provider._send_request') as provider:
            result = self.client.get(url, {'page':'editorial-board','fit':'contain','position':'bottom'})
        provider.assert_not_called()
        self.assertContains(result, 'See this image on a page')
        self.assertContains(result, 'Editorial board (hidden)')
        self.assertContains(result, self.route('image', run))
        self.assertContains(result, 'object-fit:contain;object-position:bottom')
        self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(result['X-Robots-Tag'], 'noindex, nofollow, noarchive')
        self.assertEqual(self.client.get(url, {'fit':'invalid'}).status_code, 400)
        self.assertEqual(self.client.get(url, {'page':'missing'}).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 405)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.pages_config, before)
        self.assertIsNone(run.accepted_at); self.assertEqual(self.site.images.count(), 0)
        self.client.force_login(self.second_user())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_candidate_page_requires_a_page(self):
        self.site.pages_config = []; self.site.save(update_fields=['pages_config'])
        run, _ = self.start()
        self.assertContains(self.client.get(self.route('page_preview', run)), 'Create a page first')
        self.assertEqual(self.site.images.count(), 0)

    def test_apply_page_uses_signed_target_and_preserves_other_content(self):
        self.page_examples(); run, _ = self.start()
        original_pages = self.site.pages_config
        token = self.page_review(run, page='editorial-board', fit='contain', position='bottom')
        before_revision, release = self.site.revision, self.site.current_release_id
        with patch('publishing.image_ai_provider._send_request') as provider:
            result = self.client.post(self.route('apply_page_preview', run), {
                'confirm':'yes', 'reviewed_image':token, 'page':'about', 'fit':'cover', 'position':'top'})
        provider.assert_not_called(); self.assertEqual(result.status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.pages_config[0], original_pages[0])
        self.assertEqual(self.site.pages_config[1], {**original_pages[1], 'image_asset_id':str(run.accepted_asset_id),
            'image_alt':'A fictional landscape with circles', 'image_fit':'contain', 'image_position':'bottom'})
        self.assertEqual(self.site.revision, before_revision + 1)
        self.assertEqual(self.site.current_release_id, release)
        self.assertIsNone(self.site.hero_asset_id)
        self.assertEqual(self.site.module_image_references.count(), 1)
        self.assertEqual(self.client.post(self.route('apply_page_preview',run), {'confirm':'yes','reviewed_image':token}).status_code, 409)

    def test_page_preview_tokens_expire_and_bind_candidate_and_draft(self):
        self.page_examples(); run, _ = self.start(); url = self.route('apply_page_preview', run)
        token = self.page_review(run, page='about')
        self.assertEqual(self.client.post(url, {'reviewed_image':token}).status_code, 400)
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token + 'bad'}).status_code, 400)
        with patch('django.core.signing.time.time', return_value=9999999999):
            self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.manager)
        self.assertEqual(csrf.post(url, {'confirm':'yes','reviewed_image':token}).status_code, 403)
        self.site.pages_config[0]['body'] = 'Changed after preview'
        self.site.save(update_fields=['pages_config'])
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code, 409)
        self.assertEqual(self.site.images.count(), 0)

    def test_page_apply_handles_saved_candidate_and_revoked_permission(self):
        self.page_examples(); run, _ = self.start()
        self.assertEqual(self.client.post(self.route('accept', run), {'confirm':'yes'}).status_code, 302)
        run.refresh_from_db(); self.site.headline = 'Newer draft'; self.site.revision += 1
        self.site.save(update_fields=['headline','revision'])
        token = self.page_review(run, page='about')
        with patch('publishing.image_ai_views.MAX_SITE_IMAGES',0):
            self.assertEqual(self.client.post(self.route('apply_page_preview', run), {'confirm':'yes','reviewed_image':token}).status_code, 302)
        self.site.refresh_from_db(); self.assertEqual(self.site.images.count(), 1)
        token = self.page_review(run, page='editorial-board')
        if self.kind == 'journal':
            Membership.objects.filter(journal=self.journal,user=self.manager,role=Membership.Role.SITE_MANAGER).update(active=False)
        else:
            OrganisationMembership.objects.filter(organisation=self.org,user=self.manager,role=OrganisationMembership.Role.PORTAL_MANAGER).update(active=False)
        self.assertEqual(self.client.post(self.route('apply_page_preview', run), {'confirm':'yes','reviewed_image':token}).status_code, 404)

    def test_page_apply_rolls_back_image_and_references_if_save_fails(self):
        self.page_examples(); run, _ = self.start(); token = self.page_review(run, page='about')
        with patch.object(type(self.site),'save',side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.route('apply_page_preview', run), {'confirm':'yes','reviewed_image':token})
        run.refresh_from_db(); self.site.refresh_from_db()
        self.assertIsNone(run.accepted_at); self.assertIsNotNone(run.image_data)
        self.assertEqual(self.site.images.count(), 0); self.assertEqual(self.site.module_image_references.count(), 0)

    def test_page_application_does_not_change_the_published_page(self):
        self.page_examples()
        manage = 'manage_journal_site' if self.kind == 'journal' else 'manage_publisher_portal'
        publish = 'publish_journal_site' if self.kind == 'journal' else 'publish_publisher_portal'
        data = self.client.get(reverse(manage, args=[self.slug()])).context
        self.assertEqual(self.client.post(reverse(publish, args=[self.slug()]), {
            'revision':str(data['site'].revision), 'fingerprint':data['fingerprint'],
            'confirm':'yes', 'synthetic':'yes'}).status_code, 302)
        self.site.refresh_from_db(); release_id = self.site.current_release_id
        run, _ = self.start(); token = self.page_review(run, page='about')
        self.assertEqual(self.client.post(self.route('apply_page_preview', run), {'confirm':'yes','reviewed_image':token}).status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.current_release_id, release_id)
        self.assertNotIn('image_asset_id', self.site.current_release.pages_config[0])
        public_page = 'public_journal_page' if self.kind == 'journal' else 'public_publisher_page'
        public_image = 'public_journal_image' if self.kind == 'journal' else 'public_portal_image'
        self.assertNotContains(Client().get(reverse(public_page, args=[self.slug(), 'about'])), 'A fictional landscape with circles')
        self.assertEqual(Client().get(reverse(public_image,args=[self.slug(),run.accepted_asset_id])).status_code,404)

    def test_discard_clears_private_candidate_without_saving(self):
        run,_=self.start()
        self.assertEqual(self.client.post(self.route('discard',run),{}).status_code,400)
        self.assertEqual(self.client.post(self.route('discard',run),{'confirm':'yes'}).status_code,302)
        self.assertEqual(self.client.post(self.route('discard',run),{'confirm':'yes'}).status_code,302)
        run.refresh_from_db();self.assertEqual(run.status,'discarded');self.assertIsNone(run.image_data)
        self.assertEqual(self.client.get(self.route('image',run)).status_code,404)
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,409)
        self.assertEqual(self.site.images.count(),0)

    def test_consent_policy_configuration_and_stale_sources_gate_requests(self):
        data=self.payload()
        with patch('publishing.image_ai_provider._send_request') as send:
            for changed in ({'confirm':''},{'synthetic':''},{'size':'bad'}):
                self.assertEqual(self.client.post(self.route('create'),{**data,**changed}).status_code,400)
            with override_settings(IPS_OPENAI_IMAGE_ENABLED=False):
                self.assertEqual(self.client.post(self.route('create'),data).status_code,409)
            setattr(self.policy,self.policy_field(),False);self.policy.save(update_fields=[self.policy_field()])
            self.assertEqual(self.client.post(self.route('create'),data).status_code,409)
            setattr(self.policy,self.policy_field(),True);self.policy.save(update_fields=[self.policy_field()])
            self.assertEqual(self.client.post(self.route('create'),{**data,'fingerprint':'stale'}).status_code,409)
            self.assertEqual(self.client.post(self.route('create'),{**data,'policy_revision':'999'}).status_code,409)
            send.assert_not_called()
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.manager)
        self.assertEqual(csrf.post(self.route('create'),data).status_code,403)
        self.assertFalse(SiteImageGenerationRun.objects.exists())

    def test_policy_change_during_generation_discards_candidate(self):
        data=self.payload()
        def change_policy(req):
            setattr(self.policy,self.policy_field(),False)
            self.policy.revision+=1;self.policy.save(update_fields=[self.policy_field(),'revision'])
            return response()
        with patch('publishing.image_ai_provider._send_request',side_effect=change_policy):
            self.assertEqual(self.client.post(self.route('create'),data).status_code,302)
        run=SiteImageGenerationRun.objects.get();self.assertEqual(run.status,'cancelled')
        self.assertIsNone(run.image_data);self.assertEqual(self.site.images.count(),0)

    def test_provider_failure_keeps_a_generic_failed_record(self):
        data=self.payload()
        with patch('publishing.image_ai_provider._send_request',return_value={'data':[{'b64_json':'invalid'}]}):
            self.assertEqual(self.client.post(self.route('create'),data).status_code,302)
        run=SiteImageGenerationRun.objects.get()
        self.assertEqual((run.status,run.error_code),('failed','provider_unavailable'))
        self.assertIsNone(run.image_data)
        self.assertEqual(self.client.get(self.route('image',run)).status_code,404)
        self.assertContains(self.client.get(self.route('result',run)),'could not be generated')

    def test_stale_or_corrupt_candidate_cannot_be_saved(self):
        run,_=self.start();self.site.headline='Changed';self.site.revision+=1
        self.site.save(update_fields=['headline','revision'])
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,409)
        self.site.refresh_from_db();self.site.headline='SECRET SITE HEADLINE' if self.kind=='journal' else 'SECRET PORTAL HEADLINE'
        self.site.revision=run.site_revision;self.site.save(update_fields=['headline','revision'])
        run.sha256='0'*64;run.save(update_fields=['sha256'])
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,409)
        run.sha256=hashlib.sha256(bytes(run.image_data)).hexdigest();run.width=None
        run.save(update_fields=['sha256','width'])
        self.assertEqual(self.client.get(self.route('image',run)).status_code,404)
        self.assertEqual(self.client.post(self.route('accept',run),{'confirm':'yes'}).status_code,409)
        self.assertEqual(self.site.images.count(),0)

    def test_candidate_site_preview_is_private_read_only_and_configurable(self):
        run, _ = self.start()
        before = (self.site.revision, self.site.hero_asset_id, self.site.hero_alt, self.site.hero_fit, self.site.hero_position)
        preview_url = self.route('site_preview', run)
        self.assertContains(self.client.get(self.route('result', run)), preview_url)
        with patch('publishing.image_ai_provider._send_request') as send:
            page = self.client.get(preview_url, {'fit':'contain', 'position':'bottom'})
        send.assert_not_called()
        self.assertEqual(page.status_code, 200)
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(page['X-Robots-Tag'], 'noindex, nofollow, noarchive')
        self.assertContains(page, 'See this image on your website')
        self.assertContains(page, self.route('image', run))
        self.assertContains(page, 'object-fit:contain;object-position:bottom')
        self.assertContains(page, self.route('result', run))
        self.assertEqual(self.client.get(preview_url, {'fit':'url(javascript:alert(1))'}).status_code, 400)
        self.assertEqual(self.client.get(preview_url, {'position':'invalid'}).status_code, 400)
        self.assertEqual(self.client.post(preview_url).status_code, 405)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual((self.site.revision, self.site.hero_asset_id, self.site.hero_alt, self.site.hero_fit, self.site.hero_position), before)
        self.assertIsNone(run.accepted_at); self.assertEqual(self.site.images.count(), 0)
        self.client.force_login(self.second_user())
        self.assertEqual(self.client.get(preview_url).status_code, 404)
        self.client.force_login(self.manager)
        self.site.headline = 'Changed draft'; self.site.revision += 1
        self.site.save(update_fields=['headline','revision'])
        self.assertEqual(self.client.get(preview_url).status_code, 409)
        self.assertNotContains(self.client.get(self.route('result', run)), preview_url)

    def test_preview_overlays_existing_hero_without_changing_it(self):
        scope = {'journal_site':self.site} if self.kind == 'journal' else {'portal_site':self.site}
        data = normalise_image(SimpleUploadedFile('existing.png', image_helpers.image_bytes(), content_type='image/png'))
        asset = SiteImageAsset.objects.create(**scope, uploaded_by=self.manager, default_alt='Existing fictional hero', **data)
        self.site.hero_asset = asset; self.site.hero_alt = 'Existing fictional hero'
        self.site.hero_fit = 'contain'; self.site.hero_position = 'top'
        self.site.save(update_fields=['hero_asset','hero_alt','hero_fit','hero_position'])
        run, _ = self.start()
        before = (self.site.revision, self.site.hero_asset_id, self.site.hero_alt, self.site.hero_fit, self.site.hero_position)
        page = self.client.get(self.route('site_preview', run))
        self.assertContains(page, self.route('image', run))
        self.assertContains(page, 'alt="A fictional landscape with circles"')
        self.site.refresh_from_db()
        self.assertEqual((self.site.revision, self.site.hero_asset_id, self.site.hero_alt, self.site.hero_fit, self.site.hero_position), before)

    def test_saved_candidate_can_preview_current_draft_until_removed(self):
        run, _ = self.start()
        self.assertEqual(self.client.post(self.route('accept',run), {'confirm':'yes'}).status_code, 302)
        run.refresh_from_db(); self.site.headline = 'A newer fictional draft'; self.site.revision += 1
        self.site.save(update_fields=['headline','revision'])
        page = self.client.get(self.route('site_preview', run))
        self.assertContains(page, 'A newer fictional draft')
        self.assertContains(page, self.route('image', run))
        run.accepted_asset.delete()
        self.assertEqual(self.client.get(self.route('site_preview', run)).status_code, 404)
        self.assertNotContains(self.client.get(self.route('result', run)), self.route('site_preview', run))

    def test_discarded_or_damaged_candidate_cannot_be_previewed(self):
        run, _ = self.start()
        run.width = None; run.save(update_fields=['width'])
        self.assertEqual(self.client.get(self.route('site_preview', run)).status_code, 404)
        self.assertNotContains(self.client.get(self.route('result', run)), self.route('site_preview', run))
        run.status = 'discarded'; run.image_data = None; run.save(update_fields=['status','image_data'])
        self.assertEqual(self.client.get(self.route('site_preview', run)).status_code, 404)

    def reviewed(self, run, fit='contain', position='bottom'):
        page = self.client.get(self.route('site_preview', run), {'fit':fit, 'position':position})
        self.assertEqual(page.status_code, 200)
        return page.context['candidate_reviewed']

    def test_apply_preview_saves_candidate_and_exact_reviewed_settings(self):
        run, _ = self.start()
        token = self.reviewed(run)
        before_revision = self.site.revision
        before_release = self.site.current_release_id
        before_content = (self.site.headline, self.site.sections_config, self.site.pages_config, self.site.design_config)
        with patch('publishing.image_ai_provider._send_request') as provider:
            result = self.client.post(self.route('apply_preview', run), {'confirm':'yes', 'reviewed_image':token,
                                      'fit':'cover', 'position':'top'})
        provider.assert_not_called()
        self.assertEqual(result.status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.revision, before_revision + 1)
        self.assertEqual(self.site.current_release_id, before_release)
        self.assertEqual((self.site.headline, self.site.sections_config, self.site.pages_config, self.site.design_config), before_content)
        self.assertEqual(self.site.hero_asset_id, run.accepted_asset_id)
        self.assertEqual((self.site.hero_alt,self.site.hero_fit,self.site.hero_position), ('A fictional landscape with circles','contain','bottom'))
        self.assertEqual(self.site.images.count(), 1)
        self.assertIsNotNone(run.accepted_at); self.assertIsNone(run.image_data)
        self.assertContains(self.client.get(self.route('result', run)), 'selected in the current draft')
        history = reverse('journal_design_history' if self.kind == 'journal' else 'publisher_design_history', args=[self.slug()])
        self.assertContains(self.client.get(history), 'Selected in draft')
        self.assertEqual(self.client.post(self.route('apply_preview', run), {'confirm':'yes','reviewed_image':token}).status_code, 409)
        self.assertEqual(self.site.images.count(), 1)

    def test_apply_existing_image_reuses_asset_and_allows_noop_replay(self):
        run, _ = self.start()
        self.assertEqual(self.client.post(self.route('accept',run), {'confirm':'yes'}).status_code, 302)
        run.refresh_from_db(); token = self.reviewed(run, 'cover', 'top')
        with patch('publishing.image_ai_views.MAX_SITE_IMAGES', 0):
            self.assertEqual(self.client.post(self.route('apply_preview',run), {'confirm':'yes','reviewed_image':token}).status_code, 302)
        self.site.refresh_from_db(); self.assertEqual(self.site.images.count(), 1)
        self.assertEqual(self.site.hero_asset_id, run.accepted_asset_id)
        revision = self.site.revision; token2 = self.reviewed(run, 'cover', 'top')
        self.assertEqual(self.client.post(self.route('apply_preview',run), {'confirm':'yes','reviewed_image':token2}).status_code, 302)
        self.site.refresh_from_db(); self.assertEqual(self.site.revision, revision)

    def test_applying_to_draft_does_not_change_published_release(self):
        manage_name = 'manage_journal_site' if self.kind == 'journal' else 'manage_publisher_portal'
        publish_name = 'publish_journal_site' if self.kind == 'journal' else 'publish_publisher_portal'
        page = self.client.get(reverse(manage_name, args=[self.slug()]))
        self.assertEqual(self.client.post(reverse(publish_name, args=[self.slug()]), {
            'revision':str(page.context['site'].revision), 'fingerprint':page.context['fingerprint'],
            'confirm':'yes', 'synthetic':'yes'}).status_code,302)
        self.site.refresh_from_db(); release_id = self.site.current_release_id
        self.assertIsNotNone(release_id); self.assertIsNone(self.site.current_release.hero_asset_id)
        run, _ = self.start(); token = self.reviewed(run)
        self.assertEqual(self.client.post(self.route('apply_preview',run), {'confirm':'yes','reviewed_image':token}).status_code,302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.current_release_id,release_id)
        self.assertIsNone(self.site.current_release.hero_asset_id)
        public_name = 'public_journal_image' if self.kind == 'journal' else 'public_portal_image'
        self.assertEqual(Client().get(reverse(public_name,args=[self.slug(),run.accepted_asset_id])).status_code,404)

    def test_apply_requires_confirmation_valid_token_csrf_and_ownership(self):
        run, _ = self.start(); token = self.reviewed(run)
        url = self.route('apply_preview',run)
        self.assertEqual(self.client.get(url).status_code,405)
        self.assertEqual(self.client.post(url, {'reviewed_image':token}).status_code,400)
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token+'x'}).status_code,400)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.manager)
        self.assertEqual(csrf.post(url, {'confirm':'yes','reviewed_image':token}).status_code,403)
        self.client.force_login(self.second_user())
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,404)
        self.client.force_login(self.manager)
        with patch('publishing.image_ai_views.IMAGE_PREVIEW_MAX_AGE', -1):
            self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,400)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),0)

    def test_changed_draft_or_candidate_invalidates_review(self):
        run, _ = self.start(); token = self.reviewed(run); url = self.route('apply_preview',run)
        self.site.headline = 'Changed without revision'; self.site.save(update_fields=['headline'])
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,409)
        self.site.headline = 'SECRET SITE HEADLINE' if self.kind == 'journal' else 'SECRET PORTAL HEADLINE'
        self.site.save(update_fields=['headline'])
        data = normalise_image(SimpleUploadedFile('other.png', image_helpers.image_bytes(size=(20,20)), content_type='image/png'))
        for key, value in data.items(): setattr(run,key,value)
        run.save(update_fields=list(data))
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,409)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),0)

    def test_accepting_after_preview_invalidates_it(self):
        run, _ = self.start(); token = self.reviewed(run); url = self.route('apply_preview',run)
        self.assertEqual(self.client.post(self.route('accept',run), {'confirm':'yes'}).status_code,302)
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,409)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),1)

    def test_discarding_after_preview_invalidates_it(self):
        run, _ = self.start(); token = self.reviewed(run); url = self.route('apply_preview',run)
        self.assertEqual(self.client.post(self.route('discard',run), {'confirm':'yes'}).status_code,302)
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,409)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),0)

    def test_review_token_cannot_be_reused_for_another_candidate(self):
        first, _ = self.start(); token = self.reviewed(first)
        second, _ = self.start()
        self.assertEqual(self.client.post(self.route('apply_preview',second), {'confirm':'yes','reviewed_image':token}).status_code,409)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),0)

    def test_full_library_and_revoked_role_block_apply(self):
        run, _ = self.start(); token = self.reviewed(run); url = self.route('apply_preview',run)
        with patch('publishing.image_ai_views.MAX_SITE_IMAGES',0):
            self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,409)
        if self.kind == 'journal':
            Membership.objects.filter(journal=self.journal,user=self.manager,role=Membership.Role.SITE_MANAGER).update(active=False)
        else:
            OrganisationMembership.objects.filter(organisation=self.org,user=self.manager,role=OrganisationMembership.Role.PORTAL_MANAGER).update(active=False)
        self.assertEqual(self.client.post(url, {'confirm':'yes','reviewed_image':token}).status_code,404)
        self.site.refresh_from_db(); self.assertIsNone(self.site.hero_asset_id); self.assertEqual(self.site.images.count(),0)

    def test_apply_rolls_back_library_and_candidate_when_draft_save_fails(self):
        run, _ = self.start(); token = self.reviewed(run)
        with patch.object(type(self.site),'save',side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.route('apply_preview',run), {'confirm':'yes','reviewed_image':token})
        run.refresh_from_db(); self.site.refresh_from_db()
        self.assertIsNone(run.accepted_at); self.assertIsNotNone(run.image_data)
        self.assertEqual(self.site.images.count(),0); self.assertIsNone(self.site.hero_asset_id)

    def test_idempotency_and_shared_daily_limit(self):
        data=self.payload();run,send=self.start(data)
        with patch('publishing.image_ai_provider._send_request',return_value=response()) as send:
            self.assertEqual(self.client.post(self.route('create'),data).status_code,302)
            send.assert_not_called()
            for _ in range(2):
                self.assertEqual(self.client.post(self.route('create'),self.payload()).status_code,302)
            self.assertEqual(self.client.post(self.route('create'),self.payload()).status_code,429)
            self.assertEqual(send.call_count,2)
        self.assertEqual(SiteImageGenerationRun.objects.count(),3)


@override_settings(DEBUG=True,IPS_LOCAL_PUBLICATION_ENABLED=True,IPS_OPENAI_IMAGE_ENABLED=True,
    IPS_OPENAI_IMAGE_MODEL='gpt-image-2.5-flare',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class JournalImageWorkflowTests(ImageWorkflowChecks, TestCase):
    kind='journal'
    setUpTestData=site_helpers.SiteAITests.__dict__['setUpTestData']
    def setUp(self):
        site_helpers.SiteAITests.setUp(self)
        self.policy.site_image_enabled=True;self.policy.save(update_fields=['site_image_enabled'])


@override_settings(DEBUG=True,IPS_LOCAL_PUBLICATION_ENABLED=True,IPS_OPENAI_IMAGE_ENABLED=True,
    IPS_OPENAI_IMAGE_MODEL='gpt-image-2.5-flare',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PortalImageWorkflowTests(ImageWorkflowChecks, TestCase):
    kind='portal'
    setUpTestData=portal_helpers.PublisherPortalAITests.__dict__['setUpTestData']
    def setUp(self):
        portal_helpers.PublisherPortalAITests.setUp(self)
        self.policy.portal_image_enabled=True;self.policy.save(update_fields=['portal_image_enabled'])
