import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.design_history_views import _categories
from publishing.models import (JournalSiteAIDesignRun, Membership, OrganisationMembership, OrganisationPortalAIDesignRun, SiteImageAsset, SiteImageGenerationRun)
from publishing.site_images import normalise_image
from publishing.site_page_views import _fingerprint
from . import test_site_images as helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DesignHistoryTests(TestCase):
    setUpTestData = helpers.SiteImageTests.__dict__['setUpTestData']
    owner = helpers.SiteImageTests.owner
    route = helpers.SiteImageTests.route
    login = helpers.SiteImageTests.login

    def history(self, kind):
        return reverse('journal_design_history' if kind == 'journal' else 'publisher_design_history', args=[self.owner(kind)[1]])

    def make_run(self, kind, version, status='pending', user=None, revision=None, applied=None, page=''):
        manager, _, site = self.owner(kind)
        site.refresh_from_db()
        model = JournalSiteAIDesignRun if kind == 'journal' else OrganisationPortalAIDesignRun
        return model.objects.create(id=uuid.uuid4(), site=site, requested_by=user or manager, prompt_version=version,
            model_name='fictional-model', policy_revision=1, site_revision=site.revision if revision is None else revision,
            site_fingerprint=_fingerprint(site, kind), brief_fingerprint='f'*64, status=status,
            target_page_slug=page, applied_revision=applied)

    def test_each_kind_filters_and_links_to_correct_private_result(self):
        for kind in ('journal', 'portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                runs={name:self.make_run(kind, item[1][0], page='author-guidelines' if name in ('page','blocks','layout') else '')
                      for name,item in _categories(kind).items()}
                page=self.client.get(self.history(kind))
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'AI design workspace')
                self.assertEqual(page.context['counts'], {'total':5,'completed':0,'applied':0})
                self.assertEqual(page['X-Robots-Tag'], 'noindex, nofollow, noarchive')
                self.assertIn('no-store', page['Cache-Control'])
                for name, run in runs.items():
                    filtered=self.client.get(self.history(kind), {'type':name})
                    self.assertEqual([row['run'].pk for row in filtered.context['rows']], [run.pk])
                    url=reverse(_categories(kind)[name][2], args=[self.owner(kind)[1], run.pk])
                    self.assertContains(filtered, url)
                    self.assertEqual(self.client.get(url).status_code, 200)
                manage=self.client.get(self.route(kind,'manage'))
                self.assertContains(manage, self.history(kind))
                self.assertContains(manage, 'Page layout')
                self.assertContains(manage, reverse(_categories(kind)['layout'][2], args=[self.owner(kind)[1], runs['layout'].pk]))

    def test_requester_and_active_role_isolation(self):
        for kind in ('journal', 'portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                other=self.other_journal_manager if kind=='journal' else self.other_portal_manager
                if kind=='journal':
                    Membership.objects.create(journal=self.journal,user=other,role=Membership.Role.SITE_MANAGER)
                else:
                    OrganisationMembership.objects.create(organisation=self.org,user=other,role=OrganisationMembership.Role.PORTAL_MANAGER)
                own=self.make_run(kind,_categories(kind)['layout'][1][0])
                foreign=self.make_run(kind,_categories(kind)['visual'][1][0],user=other)
                page=self.client.get(self.history(kind))
                self.assertEqual([row['run'].pk for row in page.context['rows']], [own.pk])
                self.assertNotContains(page, str(foreign.pk))
                self.client.force_login(other)
                page=self.client.get(self.history(kind))
                self.assertEqual([row['run'].pk for row in page.context['rows']], [foreign.pk])
                result=reverse(_categories(kind)['layout'][2],args=[self.owner(kind)[1],own.pk])
                self.assertEqual(self.client.get(result).status_code,404)
                for user in (self.author,self.admin):
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(self.history(kind)).status_code,404)
                self.login(kind)
                if kind=='journal':
                    Membership.objects.filter(user=self.journal_manager,journal=self.journal,role=Membership.Role.SITE_MANAGER).update(active=False)
                else:
                    OrganisationMembership.objects.filter(user=self.portal_manager,organisation=self.org,role=OrganisationMembership.Role.PORTAL_MANAGER).update(active=False)
                self.assertEqual(self.client.get(self.history(kind)).status_code,404)

    def test_snapshot_applied_and_status_independence(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                self.make_run(kind,_categories(kind)['visual'][1][0],status='completed')
                self.make_run(kind,_categories(kind)['page'][1][0],status='completed',revision=999,applied=1000)
                self.make_run(kind,_categories(kind)['blocks'][1][0],status='failed',revision=999)
                page=self.client.get(self.history(kind))
                self.assertContains(page,'Same draft snapshot')
                self.assertContains(page,'Applied to draft revision 1000')
                self.assertContains(page,'Draft has changed since this request')
                self.assertContains(page,'does not guarantee')
                self.assertEqual(page.context['counts'],{'total':3,'completed':2,'applied':1})
                filtered=self.client.get(self.history(kind),{'status':'failed'})
                self.assertEqual(len(filtered.context['rows']),1)
                self.assertEqual(filtered.context['rows'][0]['category'],'blocks')
                invalid=self.client.get(self.history(kind),{'type':'invalid','status':'invalid'})
                self.assertEqual((invalid.context['selected_type'],invalid.context['selected_status']),('all','all'))

    def test_legacy_and_unknown_types_are_handled_without_wrong_links(self):
        self.login('journal')
        self.make_run('journal',_categories('journal')['visual'][1][1])
        self.make_run('journal',_categories('journal')['blocks'][1][0])
        self.make_run('journal','future-unknown-kind')
        page=self.client.get(self.history('journal'),{'type':'other'})
        self.assertEqual(len(page.context['rows']),1)
        self.assertIsNone(page.context['rows'][0]['result_url'])
        self.assertContains(page,'This request type has no review page.')
        self.assertEqual(len(self.client.get(self.history('journal'),{'type':'visual'}).context['rows']),1)
        self.assertEqual(len(self.client.get(self.history('journal'),{'type':'blocks'}).context['rows']),1)

    def test_pagination_is_filtered_and_view_does_not_dispatch_or_write(self):
        self.login('journal')
        for _ in range(13): self.make_run('journal',_categories('journal')['layout'][1][0])
        with patch('publishing.ai_provider._send_request') as ai:
            page=self.client.get(self.history('journal'),{'type':'layout'})
            next_page=self.client.get(self.history('journal'),{'type':'layout','page':2})
        ai.assert_not_called()
        self.assertEqual(len(mail.outbox),0)
        self.assertEqual(page.context['page'].paginator.count,13)
        self.assertEqual(len(page.context['rows']),12)
        self.assertEqual(len(next_page.context['rows']),1)
        self.assertContains(page,'type=layout&amp;status=all&amp;page=2')
        self.assertEqual(JournalSiteAIDesignRun.objects.count(),13)
        self.assertEqual(self.client.post(self.history('journal')).status_code,405)

    def test_chinese_and_local_only(self):
        for kind in ('journal','portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                self.make_run(kind,_categories(kind)['layout'][1][0])
                page=self.client.get(self.history(kind),HTTP_ACCEPT_LANGUAGE='zh-hans')
                self.assertContains(page,'AI 设计工作台')
                self.assertContains(page,'页面布局')
                with self.settings(DEBUG=False):
                    self.assertEqual(self.client.get(self.history(kind)).status_code,404)
                with self.settings(IPS_LOCAL_PUBLICATION_ENABLED=False):
                    self.assertEqual(self.client.get(self.history(kind)).status_code,404)


    def make_image(self, kind, status='completed', user=None, accepted=False, description='Fictional abstract cover'):
        manager, _, site = self.owner(kind)
        scope = {'journal_site':site} if kind == 'journal' else {'portal_site':site}
        data = normalise_image(SimpleUploadedFile('sample.png', helpers.image_bytes(), content_type='image/png'))
        asset = None
        if accepted:
            asset = SiteImageAsset.objects.create(**scope, uploaded_by=user or manager, default_alt=description, **data)
        run = SiteImageGenerationRun.objects.create(**scope, requested_by=user or manager, model_name='fictional',
            prompt_fingerprint='f'*64, image_description=description, size='1536x1024', policy_revision=1,
            site_revision=site.revision, site_fingerprint=_fingerprint(site, kind), status=status,
            accepted_at=timezone.now() if accepted else None, accepted_asset=asset,
            **({**data, 'image_data': None} if accepted else data))
        return run

    def test_images_join_scoped_history_with_real_private_links_and_statuses(self):
        for kind in ('journal', 'portal'):
            with self.subTest(kind=kind):
                self.login(kind)
                design = self.make_run(kind, _categories(kind)['visual'][1][0], status='completed')
                image = self.make_image(kind, description='Fictional <script> cover')
                accepted = self.make_image(kind, accepted=True)
                discarded = self.make_image(kind, status='discarded')
                other = self.other_journal_manager if kind == 'journal' else self.other_portal_manager
                foreign = self.make_image(kind, user=other, description='PRIVATE OTHER USER')
                with patch('publishing.image_ai_provider._send_request') as provider:
                    page = self.client.get(self.history(kind))
                provider.assert_not_called()
                self.assertEqual(page.context['counts'], {'total':4, 'completed':3, 'applied':0})
                self.assertEqual(page.context['saved_images_count'], 1)
                self.assertNotContains(page, 'PRIVATE OTHER USER')
                self.assertNotContains(page, str(foreign.pk))
                self.assertContains(page, 'Fictional &lt;script&gt; cover', html=False)
                self.assertContains(page, 'Added to image library')
                self.assertContains(page, 'Candidate discarded')
                result_route = ('journal' if kind == 'journal' else 'portal') + '_image_ai_result'
                image_route = ('journal' if kind == 'journal' else 'portal') + '_image_ai_image'
                for run in (image, accepted, discarded):
                    self.assertContains(page, reverse(result_route, args=[self.owner(kind)[1], run.pk]))
                src = reverse(image_route, args=[self.owner(kind)[1], image.pk])
                self.assertContains(page, src)
                self.assertEqual(self.client.get(src).status_code, 200)
                selected = self.client.get(self.history(kind), {'type':'image', 'status':'completed'})
                self.assertEqual({row['run'].pk for row in selected.context['rows']}, {image.pk, accepted.pk})
                selected = self.client.get(self.history(kind), {'type':'image', 'status':'discarded'})
                self.assertEqual([row['run'].pk for row in selected.context['rows']], [discarded.pk])
                self.assertEqual(self.client.get(self.history(kind), {'type':'visual', 'status':'discarded'}).context['rows'], [])
                self.assertEqual([row['run'].pk for row in self.client.get(self.history(kind), {'type':'visual'}).context['rows']], [design.pk])
                accepted.accepted_asset.delete()
                page = self.client.get(self.history(kind), {'type':'image'})
                self.assertContains(page, 'Image removed from library')
                self.assertEqual(page.context['saved_images_count'], 1)

    def test_mixed_request_pagination_is_stable_and_scoped(self):
        self.login('journal')
        now = timezone.now()
        expected = []
        for index in range(15):
            run = self.make_image('journal') if index % 2 else self.make_run('journal', _categories('journal')['visual'][1][0])
            type(run).objects.filter(pk=run.pk).update(created_at=now + timedelta(seconds=index))
            expected.append(run.pk)
        first = self.client.get(self.history('journal'))
        second = self.client.get(self.history('journal'), {'page':2})
        self.assertEqual(first.context['page'].paginator.count, 15)
        self.assertEqual([r['run'].pk for r in first.context['rows']], list(reversed(expected))[:12])
        self.assertEqual([r['run'].pk for r in second.context['rows']], list(reversed(expected))[12:])
        self.assertContains(first, 'type=all&amp;status=all&amp;page=2')
