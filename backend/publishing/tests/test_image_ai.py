import base64
import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_provider import ProviderError
from publishing.image_ai_provider import generate_image
from publishing.models import Membership, OrganisationMembership, SiteImageGenerationRun
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
