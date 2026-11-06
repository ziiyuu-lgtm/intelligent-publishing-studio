import shutil
import tempfile
import hashlib
import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.models import (EditorialAssignment, EditorialDecision, Journal, JournalIssue, Membership, Organisation, ProductionAssignment,
                               PublicationApproval, PublicationDraft, PublicationProof, PublishedIssueProposal, ReviewAssignment,
                               ReviewRound, Submission, SubmissionEvent, SubmissionVersion)
from publishing.production_views import _proof_bytes

PDF = b"%PDF-1.4\nFictional publication proof\n%%EOF\n"


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProductionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-production-tests-")
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user("production-author")
        cls.editor = users.create_user("production-editor")
        cls.producer = users.create_user("fictional-producer")
        cls.coordinator = users.create_user("production-coordinator")
        cls.other_editor = users.create_user("unassigned-editor")
        cls.reviewer = users.create_user("production-reviewer")
        cls.org = Organisation.objects.create(name="Fictional publisher", slug="production-publisher")
        cls.journal = Journal.objects.create(organisation=cls.org, name="Fictional journal", slug="production-journal", review_mode="direct")
        cls.other_journal = Journal.objects.create(organisation=cls.org, name="Other test journal", slug="production-other")
        for user, role in [(cls.author, "author"), (cls.editor, "editor"), (cls.producer, "producer"), (cls.other_editor, "editor"), (cls.reviewer, "reviewer"), (cls.coordinator, "coordinator")]:
            Membership.objects.create(user=user, journal=cls.journal, role=role)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title="Fictional original title",
            abstract="Fictional original abstract", manuscript="manuscripts/original.pdf", status="accepted")
        cls.version = SubmissionVersion.objects.create(submission=cls.submission, number=1,
            manuscript="manuscripts/original.pdf", uploaded_by=cls.author)
        cls.round = ReviewRound.objects.create(submission=cls.submission, version=cls.version, number=1, status="closed")
        cls.decision = EditorialDecision.objects.create(submission=cls.submission, round=cls.round, outcome="accept",
                                                        author_message="Accepted for this fictional test.", decided_by=cls.editor)
        EditorialAssignment.objects.create(submission=cls.submission, editor=cls.editor, assigned_by=cls.coordinator)
        ReviewAssignment.objects.create(submission=cls.submission, round=cls.round, reviewer=cls.reviewer, invited_by=cls.editor,
                                        status="submitted", conflict="none", comments="SECRET fictional review")

    def start(self):
        self.client.force_login(self.editor)
        result = self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"})
        self.assertEqual(result.status_code, 302)
        draft = PublicationDraft.objects.get(submission=self.submission)
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.producer.pk})
        self.assertEqual(response.status_code, 302)
        return draft

    def edit(self, draft, **changes):
        draft.refresh_from_db()
        self.client.force_login(self.producer)
        values = {"revision":str(draft.revision), "title":"Fictional public title", "authors":"Fictional Author",
                  "abstract":"Fictional public abstract"}
        values.update(changes)
        return self.client.post(reverse("edit_production_metadata", args=[draft.pk]), values)

    def upload(self, draft, data=PDF):
        draft.refresh_from_db()
        self.client.force_login(self.producer)
        return self.client.post(reverse("upload_publication_proof", args=[draft.pk]), {"revision":str(draft.revision),
            "synthetic":"on", "file":SimpleUploadedFile("fake.pdf", data, content_type="application/pdf")})

    def confirmation_data(self, draft, actor):
        self.client.force_login(actor)
        response = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertEqual(response.status_code, 200)
        return {"revision":str(response.context["draft"].revision), "proof":str(response.context["proof"].pk),
                "fingerprint":response.context["fingerprint"], "confirm":"yes", "synthetic":"yes"}

    def approve(self, draft):
        return self.client.post(reverse("approve_publication_proof", args=[draft.pk]), self.confirmation_data(draft, self.author))

    def publish(self, draft):
        return self.client.post(reverse("publish_test_article", args=[draft.pk]), self.confirmation_data(draft, self.producer))

    def ready(self):
        draft = self.start()
        self.assertEqual(self.edit(draft).status_code, 302)
        self.assertEqual(self.upload(draft).status_code, 302)
        return draft

    def create_issue(self, volume=1, number=1, year=2025, title="Fictional issue"):
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse("manage_test_issues", args=[self.journal.slug]),
            {"volume":volume, "number":number, "year":year, "title":title, "synthetic":"on"})
        self.assertEqual(response.status_code, 302)
        return JournalIssue.objects.get(journal=self.journal, volume=volume, number=number)

    def select_issue(self, draft, issue=None):
        draft.refresh_from_db()
        self.client.force_login(self.producer)
        return self.client.post(reverse("set_production_issue", args=[draft.pk]),
                                {"revision":str(draft.revision), "issue":str(issue.pk) if issue else ""})

    def release_issue(self, issue, actor=None):
        self.client.force_login(actor or self.coordinator)
        review = self.client.get(reverse("review_test_issue_release", args=[issue.pk]))
        token = review.context["release_token"] if review.status_code == 200 else ""
        return self.client.post(reverse("release_test_issue", args=[issue.pk]),
            {"confirm":"yes", "synthetic":"yes", "release_token":token})

    def test_default_off_and_debug_off_block_all_publication_routes(self):
        for settings_values in ({"IPS_LOCAL_PUBLICATION_ENABLED":False}, {"DEBUG":False}):
            with self.subTest(settings_values=settings_values), override_settings(**settings_values):
                self.client.force_login(self.editor)
                self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"}).status_code, 404)
                self.assertEqual(self.client.get(reverse("journal_test_articles", args=[self.journal.slug])).status_code, 404)
        self.assertFalse(PublicationDraft.objects.exists())

    def test_only_assigned_editor_can_start_after_latest_version_acceptance(self):
        for user in (self.author, self.reviewer, self.other_editor, self.coordinator, self.producer):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"}).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {}).status_code, 400)
        Submission.objects.filter(pk=self.submission.pk).update(status="submitted")
        self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"}).status_code, 409)
        Submission.objects.filter(pk=self.submission.pk).update(status="accepted")
        SubmissionVersion.objects.create(submission=self.submission, number=2, manuscript="manuscripts/new.pdf", uploaded_by=self.author)
        self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"}).status_code, 409)
        self.assertFalse(PublicationDraft.objects.exists())

    def test_private_until_author_confirmation_and_explicit_publication(self):
        draft = self.ready()
        proof = PublicationProof.objects.get(draft=draft)
        public_url = reverse("test_article", args=[self.journal.slug, draft.pk])
        pdf_url = reverse("test_article_pdf", args=[self.journal.slug, draft.pk])
        anonymous = Client()
        self.assertEqual(anonymous.get(public_url).status_code, 404)
        self.assertEqual(anonymous.get(pdf_url).status_code, 404)
        self.assertNotContains(anonymous.get(reverse("journal_test_articles", args=[self.journal.slug])), "Fictional public title")
        self.assertEqual(self.publish(draft).status_code, 409)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("private_publication_proof", args=[draft.pk, proof.pk])).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.client.force_login(self.producer)
        producer_page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertNotContains(producer_page, "SECRET fictional review")
        self.assertNotContains(producer_page, self.reviewer.username)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.client.force_login(self.author)
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertNotContains(page, "SECRET fictional review")
        self.assertNotContains(page, self.reviewer.username)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(PublicationApproval.objects.count(), 1)
        self.assertEqual(self.publish(draft).status_code, 302)
        public = anonymous.get(public_url)
        self.assertContains(public, "Fictional public title")
        self.assertContains(public, "Fictional Author")
        self.assertNotContains(public, "SECRET fictional review")
        self.assertNotContains(public, self.reviewer.username)
        self.assertEqual(public["X-Robots-Tag"], "noindex, nofollow, noarchive")
        download = anonymous.get(pdf_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), PDF)
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")
        self.assertEqual(anonymous.get(reverse("test_article", args=[self.other_journal.slug, draft.pk])).status_code, 404)
        self.assertContains(anonymous.get(reverse("journal_test_articles", args=[self.journal.slug])), "Fictional public title")
        self.assertEqual(self.publish(draft).status_code, 409)
        self.assertEqual(SubmissionEvent.objects.filter(action="test_article_published").count(), 1)
        self.assertEqual(self.edit(draft).status_code, 409)
        self.assertEqual(self.upload(draft).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 409)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(anonymous.get(public_url).status_code, 404)
        self.assertEqual(anonymous.get(pdf_url).status_code, 404)

    def test_author_confirmation_invalidated_by_metadata_and_new_proof(self):
        draft = self.ready()
        stale = self.confirmation_data(draft, self.author)
        self.assertEqual(self.edit(draft, title="Changed fictional title").status_code, 302)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("approve_publication_proof", args=[draft.pk]), stale).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.upload(draft, PDF+b"new proof").status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        self.assertEqual(PublicationApproval.objects.count(), 2)

    def test_mutated_or_missing_pdf_blocks_confirmation_and_public_download(self):
        draft = self.ready()
        proof = PublicationProof.objects.get(draft=draft)
        with open(proof.file.path, "ab") as output:
            output.write(b"tampered")
        self.assertEqual(self.approve(draft).status_code, 409)
        self.assertFalse(PublicationApproval.objects.exists())
        self.assertEqual(self.publish(draft).status_code, 409)

    def test_reverted_metadata_still_requires_a_new_confirmation(self):
        draft = self.ready()
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.edit(draft, title="Changed title").status_code, 302)
        self.assertEqual(self.edit(draft, title="Fictional public title").status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)

    def test_post_publication_pdf_tampering_blocks_public_download(self):
        draft = self.ready()
        self.approve(draft)
        self.publish(draft)
        proof = PublicationProof.objects.get(draft=draft)
        with open(proof.file.path, "ab") as output:
            output.write(b"tampered")
        self.assertEqual(Client().get(reverse("test_article_pdf", args=[self.journal.slug, draft.pk])).status_code, 404)

    def test_private_and_published_downloads_return_the_exact_verified_bytes(self):
        draft = self.ready()
        proof = PublicationProof.objects.get(draft=draft)
        changed = b"%PDF-1.4\nUnchecked replacement\n%%EOF"

        def change_after_read(current_proof):
            data = _proof_bytes(current_proof)
            Path(current_proof.file.path).write_bytes(changed)
            return data

        private_url = reverse("private_publication_proof", args=[draft.pk, proof.pk])
        self.client.force_login(self.author)
        with patch("publishing.production_views._proof_bytes", side_effect=change_after_read):
            response = self.client.get(private_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertEqual(self.client.get(private_url).status_code, 404)

        Path(proof.file.path).write_bytes(PDF)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        public_url = reverse("test_article_pdf", args=[self.journal.slug, draft.pk])
        with patch("publishing.production_views._proof_bytes", side_effect=change_after_read):
            response = Client().get(public_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PDF)
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertEqual(Client().get(public_url).status_code, 404)

    def test_invalid_or_oversized_proof_is_rejected_even_if_its_hash_matches(self):
        from publishing.forms import MAX_MANUSCRIPT_BYTES

        draft = self.ready()
        proof = PublicationProof.objects.get(draft=draft)
        private_url = reverse("private_publication_proof", args=[draft.pk, proof.pk])
        self.client.force_login(self.author)
        for data in (b"not a PDF", b"%PDF-" + b"x" * MAX_MANUSCRIPT_BYTES):
            with self.subTest(size=len(data)):
                Path(proof.file.path).write_bytes(data)
                proof.sha256 = hashlib.sha256(data).hexdigest()
                proof.save(update_fields=["sha256"])
                self.assertEqual(self.client.get(private_url).status_code, 404)
                self.assertEqual(self.approve(draft).status_code, 409)

    def test_public_metadata_is_escaped(self):
        draft = self.start()
        self.assertEqual(self.edit(draft, title="<script>alert(1)</script>").status_code, 302)
        self.assertEqual(self.upload(draft).status_code, 302)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        page = Client().get(reverse("test_article", args=[self.journal.slug, draft.pk]))
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(page, "<script>alert(1)</script>")

    def test_production_continues_from_editorial_acceptance(self):
        self.decision.delete()
        ReviewRound.objects.filter(pk=self.round.pk).update(status="open")
        Submission.objects.filter(pk=self.submission.pk).update(status="under_review")
        self.client.force_login(self.editor)
        response = self.client.post(reverse("editorial_decision", args=[self.submission.pk]),
                                    {"outcome":"accept", "author_message":"Fictional acceptance"})
        self.assertEqual(response.status_code, 302)
        draft = self.ready()
        self.assertEqual(draft.decision.outcome, "accept")
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)

    def test_simplified_chinese_production_page(self):
        draft = self.start()
        self.client.force_login(self.producer)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertContains(page, "制作工作台")
        self.assertContains(page, "公开元数据预览")
        self.assertContains(page, "下载录用原稿 PDF")

    def test_invalid_pdf_and_failed_transaction_leave_no_new_proof(self):
        from pathlib import Path
        before = set(Path(self.media_dir).rglob("*.pdf"))
        draft = self.start()
        self.edit(draft)
        self.assertEqual(self.upload(draft, b"not a PDF").status_code, 400)
        self.assertFalse(PublicationProof.objects.exists())
        with patch("publishing.production_views._event", side_effect=RuntimeError("synthetic error")):
            with self.assertRaises(RuntimeError):
                self.upload(draft)
        self.assertFalse(PublicationProof.objects.exists())
        self.assertEqual(set(Path(self.media_dir).rglob("*.pdf")), before)

    def test_removing_producer_assignment_and_disabling_journal_block_access(self):
        draft = self.ready()
        ProductionAssignment.objects.filter(producer=self.producer, draft=draft).update(status="revoked")
        self.client.force_login(self.producer)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.edit(draft).status_code, 404)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(Client().get(reverse("journal_test_articles", args=[self.journal.slug])).status_code, 404)

    def test_csrf_protects_approval_and_publication(self):
        draft = self.ready()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post(reverse("approve_publication_proof", args=[draft.pk]), {}).status_code, 403)
        client.force_login(self.producer)
        self.assertEqual(client.post(reverse("publish_test_article", args=[draft.pk]), {}).status_code, 403)
        self.assertEqual(client.get(reverse("publish_test_article", args=[draft.pk])).status_code, 405)
        self.assertFalse(PublicationApproval.objects.exists())

    def test_producer_needs_explicit_assignment_and_cannot_read_reviews(self):
        version = SubmissionVersion.objects.get(pk=self.version.pk)
        version.manuscript.save("source.pdf", ContentFile(b"%PDF-1.4\naccepted source\n%%EOF"), save=True)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("start_production", args=[self.submission.pk]), {"synthetic":"yes"}).status_code, 302)
        draft = PublicationDraft.objects.get(submission=self.submission)
        self.client.force_login(self.producer)
        self.assertNotContains(self.client.get(reverse("workspace")), draft.title)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("accepted_production_source", args=[draft.pk])).status_code, 404)
        self.client.force_login(self.coordinator)
        team = self.client.get(reverse("production_team", args=[self.submission.pk]))
        self.assertContains(team, self.submission.title)
        self.assertNotContains(team, self.submission.abstract)
        self.assertNotContains(team, "SECRET fictional review")
        self.assertNotContains(team, self.reviewer.username)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.producer.pk}).status_code, 302)
        self.client.force_login(self.producer)
        self.assertContains(self.client.get(reverse("workspace")), draft.title)
        source = self.client.get(reverse("accepted_production_source", args=[draft.pk]))
        self.assertEqual(source.status_code, 200)
        self.assertIn(b"accepted source", b"".join(source.streaming_content))
        self.assertEqual(source["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("review_detail", args=[self.submission.reviews.first().pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[self.submission.pk]), {"outcome":"reject", "author_message":"No"}).status_code, 404)

    def test_assignment_rejects_conflicts_cross_journal_and_self_assignment(self):
        draft = self.start()
        Membership.objects.create(user=self.author, journal=self.journal, role=Membership.Role.PRODUCER)
        Membership.objects.create(user=self.reviewer, journal=self.journal, role=Membership.Role.PRODUCER)
        Membership.objects.create(user=self.coordinator, journal=self.journal, role=Membership.Role.PRODUCER)
        foreign = get_user_model().objects.create_user("foreign-producer")
        Membership.objects.create(user=foreign, journal=self.other_journal, role=Membership.Role.PRODUCER)
        recused = get_user_model().objects.create_user("recused-producer")
        Membership.objects.create(user=recused, journal=self.journal, role=Membership.Role.PRODUCER)
        EditorialAssignment.objects.create(submission=self.submission, editor=recused, assigned_by=self.coordinator, status="recused")
        self.client.force_login(self.coordinator)
        for user in (self.author, self.reviewer, self.coordinator, foreign, recused):
            self.assertEqual(self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":user.pk}).status_code, 400)
        self.assertEqual(self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.producer.pk}).status_code, 302)
        self.assertEqual(ProductionAssignment.objects.filter(draft=draft).count(), 1)
        self.assertEqual(SubmissionEvent.objects.filter(action="production_staff_assigned").count(), 1)
        Membership.objects.create(user=foreign, journal=self.other_journal, role=Membership.Role.COORDINATOR)
        self.client.force_login(foreign)
        self.assertEqual(self.client.get(reverse("production_team", args=[self.submission.pk])).status_code, 404)

    def test_production_recusal_blocks_editorial_and_coordination_access_and_preserves_history(self):
        draft = self.ready()
        self.assertEqual(self.approve(draft).status_code, 302)
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.PRODUCER)
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.COORDINATOR)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.editor.pk}).status_code, 302)
        assignment = ProductionAssignment.objects.get(draft=draft, producer=self.editor)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("recuse_producer", args=[assignment.pk]), {}).status_code, 400)
        self.assertEqual(self.client.post(reverse("recuse_producer", args=[assignment.pk]), {"confirm":"yes"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("production_team", args=[self.submission.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("workspace")), draft.title)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.editor.pk}).status_code, 400)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "recused")
        self.assertEqual(PublicationApproval.objects.count(), 1)
        self.assertTrue(EditorialDecision.objects.filter(pk=self.decision.pk).exists())

    def test_editor_recusal_blocks_separately_assigned_production_access(self):
        draft = self.start()
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.PRODUCER)
        self.client.force_login(self.coordinator)
        self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.editor.pk})
        editor_assignment = EditorialAssignment.objects.get(submission=self.submission, editor=self.editor)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("recuse_editor", args=[editor_assignment.pk]), {"confirm":"yes"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("workspace")), draft.title)

    def test_inconsistent_review_assignment_cannot_bypass_producer_restrictions(self):
        draft = self.start()
        Membership.objects.create(user=self.producer, journal=self.journal, role=Membership.Role.REVIEWER)
        review = ReviewAssignment.objects.create(submission=self.submission, round=self.round, reviewer=self.producer,
            invited_by=self.editor, status="accepted", conflict="none")
        self.client.force_login(self.producer)
        self.assertEqual(self.client.get(reverse("review_detail", args=[review.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)

    def test_production_assignment_and_recusal_are_csrf_protected(self):
        draft = self.start()
        assignment = ProductionAssignment.objects.get(draft=draft, producer=self.producer)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)
        self.assertEqual(client.post(reverse("production_team", args=[self.submission.pk]), {"producer":self.producer.pk}).status_code, 403)
        self.assertEqual(client.post(reverse("remove_production_assignment", args=[assignment.pk])).status_code, 403)
        client.force_login(self.producer)
        self.assertEqual(client.post(reverse("recuse_producer", args=[assignment.pk]), {"confirm":"yes"}).status_code, 403)
        self.assertEqual(client.get(reverse("recuse_producer", args=[assignment.pk])).status_code, 405)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "active")

    def test_production_assignment_removal_revokes_source_and_proof_access(self):
        draft = self.ready()
        proof = PublicationProof.objects.get(draft=draft)
        assignment = ProductionAssignment.objects.get(draft=draft, producer=self.producer)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("remove_production_assignment", args=[assignment.pk])).status_code, 302)
        self.assertEqual(self.client.post(reverse("remove_production_assignment", args=[assignment.pk])).status_code, 409)
        self.client.force_login(self.producer)
        for name, args in [("production_detail", [draft.pk]), ("accepted_production_source", [draft.pk]),
                           ("private_publication_proof", [draft.pk, proof.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
        self.assertNotContains(self.client.get(reverse("workspace")), draft.title)
        self.assertEqual(PublicationProof.objects.filter(pk=proof.pk).count(), 1)

    def test_assignment_removal_after_publication_does_not_withdraw_test_article(self):
        draft = self.ready()
        self.approve(draft)
        self.publish(draft)
        assignment = ProductionAssignment.objects.get(draft=draft, producer=self.producer)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("remove_production_assignment", args=[assignment.pk])).status_code, 302)
        self.client.force_login(self.producer)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("accepted_production_source", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("test_article", args=[self.journal.slug, draft.pk])).status_code, 200)

    def test_disabled_producer_role_and_oversized_identifier_fail_closed(self):
        draft = self.start()
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse("production_team", args=[self.submission.pk]), {"producer":"9"*100})
        self.assertEqual(response.status_code, 400)
        Membership.objects.filter(user=self.producer, journal=self.journal, role="producer").update(active=False)
        self.client.force_login(self.producer)
        self.assertEqual(self.client.get(reverse("production_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("accepted_production_source", args=[draft.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("workspace")), draft.title)

    def test_issue_creation_is_scoped_unique_and_immutable(self):
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("manage_test_issues", args=[self.journal.slug])).status_code, 404)
        self.client.force_login(self.coordinator)
        url = reverse("manage_test_issues", args=[self.journal.slug])
        self.assertEqual(self.client.post(url, {"volume":0, "number":1, "year":2025, "synthetic":"on"}).status_code, 400)
        issue = self.create_issue()
        self.assertEqual(self.client.post(url, {"volume":1, "number":1, "year":2025, "title":"Fictional issue", "synthetic":"on"}).status_code, 302)
        self.assertEqual(JournalIssue.objects.filter(journal=self.journal).count(), 1)
        self.assertEqual(self.client.post(url, {"volume":1, "number":1, "year":2026, "title":"Different", "synthetic":"on"}).status_code, 409)
        issue.refresh_from_db()
        self.assertEqual((issue.year, issue.title), (2025, "Fictional issue"))
        self.assertEqual(self.client.get(reverse("manage_test_issues", args=[self.other_journal.slug])).status_code, 404)
        foreign_coordinator = get_user_model().objects.create_user("other-issue-coordinator")
        Membership.objects.create(user=foreign_coordinator, journal=self.other_journal, role=Membership.Role.COORDINATOR)
        self.client.force_login(foreign_coordinator)
        result = self.client.post(reverse("manage_test_issues", args=[self.other_journal.slug]),
                                  {"volume":1, "number":1, "year":2025, "title":"Other issue", "synthetic":"on"})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(JournalIssue.objects.filter(volume=1, number=1).count(), 2)

    def test_issue_assignment_and_release_follow_publication_and_journal_scope(self):
        draft = self.ready()
        issue = self.create_issue()
        self.assertEqual(self.select_issue(draft, issue).status_code, 302)
        draft.refresh_from_db()
        revision = draft.revision
        self.assertEqual(self.select_issue(draft, issue).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, revision)
        self.assertEqual(SubmissionEvent.objects.filter(submission=self.submission, action="test_issue_selection_changed").count(), 1)
        self.assertEqual(self.release_issue(issue).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        public = Client()
        article = public.get(reverse("test_article", args=[self.journal.slug, draft.pk]))
        self.assertContains(article, "Volume 1")
        self.assertContains(article, "Issue 1")
        issue_url = reverse("test_issue", args=[self.journal.slug, issue.pk])
        self.assertEqual(public.get(issue_url).status_code, 404)
        self.assertEqual(self.release_issue(issue).status_code, 302)
        page = public.get(issue_url)
        self.assertContains(page, "Fictional public title")
        self.assertNotContains(page, "SECRET fictional review")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertContains(public.get(reverse("journal_test_articles", args=[self.journal.slug])), "Fictional issue")
        self.assertEqual(public.get(reverse("test_issue", args=[self.other_journal.slug, issue.pk])).status_code, 404)
        self.assertEqual(self.release_issue(issue).status_code, 409)
        self.assertEqual(self.select_issue(draft).status_code, 409)

    def test_issue_selection_invalidates_author_confirmation_and_stale_forms(self):
        draft = self.ready()
        issue = self.create_issue()
        old_data = self.confirmation_data(draft, self.author)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.select_issue(draft, issue).status_code, 302)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("approve_publication_proof", args=[draft.pk]), old_data).status_code, 409)
        self.assertEqual(self.publish(draft).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.select_issue(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 409)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        self.assertEqual(PublicationApproval.objects.count(), 3)

    def test_cross_journal_issue_and_unassigned_actor_cannot_change_draft(self):
        draft = self.ready()
        issue = JournalIssue.objects.create(journal=self.other_journal, volume=1, number=1, year=2025, created_by=self.coordinator)
        self.assertEqual(self.select_issue(draft, issue).status_code, 400)
        draft.refresh_from_db()
        self.assertIsNone(draft.issue_id)
        self.client.force_login(self.producer)
        url = reverse("set_production_issue", args=[draft.pk])
        self.assertEqual(self.client.post(url, {"revision":draft.revision, "issue":"not-a-uuid"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"revision":draft.revision}).status_code, 400)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.post(url, {"revision":draft.revision, "issue":""}).status_code, 404)

    def test_issue_release_requires_independent_coordinator_and_no_unpublished_articles(self):
        issue = self.create_issue()
        self.assertEqual(self.release_issue(issue).status_code, 409)
        draft = self.ready()
        self.select_issue(draft, issue)
        self.assertEqual(self.release_issue(issue).status_code, 409)
        self.approve(draft)
        self.publish(draft)
        for user in (self.author, self.reviewer):
            Membership.objects.create(user=user, journal=self.journal, role=Membership.Role.COORDINATOR)
            self.assertEqual(self.release_issue(issue, user).status_code, 403)
        recused = get_user_model().objects.create_user("recused-issue-coordinator")
        Membership.objects.create(user=recused, journal=self.journal, role=Membership.Role.COORDINATOR)
        EditorialAssignment.objects.create(submission=self.submission, editor=recused, assigned_by=self.coordinator, status="recused")
        self.assertEqual(self.release_issue(issue, recused).status_code, 403)
        recused_producer = get_user_model().objects.create_user("recused-issue-producer")
        Membership.objects.create(user=recused_producer, journal=self.journal, role=Membership.Role.COORDINATOR)
        ProductionAssignment.objects.create(draft=draft, producer=recused_producer, assigned_by=self.coordinator, status="recused")
        self.assertEqual(self.release_issue(issue, recused_producer).status_code, 403)
        self.assertEqual(self.release_issue(issue).status_code, 302)

    def test_issue_mutations_are_csrf_protected_and_disabled_by_default(self):
        draft = self.ready()
        issue = self.create_issue()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)
        self.assertEqual(client.post(reverse("manage_test_issues", args=[self.journal.slug]), {}).status_code, 403)
        self.assertEqual(client.post(reverse("release_test_issue", args=[issue.pk]), {}).status_code, 403)
        client.force_login(self.producer)
        self.assertEqual(client.post(reverse("set_production_issue", args=[draft.pk]), {}).status_code, 403)
        self.assertEqual(client.get(reverse("set_production_issue", args=[draft.pk])).status_code, 405)
        with override_settings(IPS_LOCAL_PUBLICATION_ENABLED=False):
            self.assertEqual(self.client.get(reverse("test_issue", args=[self.journal.slug, issue.pk])).status_code, 404)
            self.client.force_login(self.coordinator)
            self.assertEqual(self.client.get(reverse("manage_test_issues", args=[self.journal.slug])).status_code, 404)

    def test_online_first_fingerprint_preserves_existing_approvals(self):
        draft = self.ready()
        draft.refresh_from_db()
        proof = draft.proofs.first()
        old_payload = {"draft": str(draft.pk), "revision": draft.revision, "title": draft.title,
                       "authors": draft.authors, "abstract": draft.abstract, "proof": proof.pk, "sha256": proof.sha256}
        legacy_hash = hashlib.sha256(json.dumps(old_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.client.force_login(self.author)
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertEqual(page.context["fingerprint"], legacy_hash)
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)

    def test_released_issue_cannot_receive_another_draft(self):
        draft = self.ready()
        issue = self.create_issue()
        self.select_issue(draft, issue)
        self.approve(draft)
        self.publish(draft)
        self.release_issue(issue)
        second = Submission.objects.create(journal=self.journal, author=self.author, title="Second fictional manuscript",
            abstract="Fictional abstract", manuscript="manuscripts/second.pdf", status="accepted")
        version = SubmissionVersion.objects.create(submission=second, number=1, manuscript="manuscripts/second.pdf", uploaded_by=self.author)
        review_round = ReviewRound.objects.create(submission=second, version=version, number=1, status="closed")
        decision = EditorialDecision.objects.create(submission=second, round=review_round, outcome="accept", author_message="Accepted", decided_by=self.editor)
        second_draft = PublicationDraft.objects.create(submission=second, decision=decision, title="Second draft", abstract="Fictional", created_by=self.editor)
        ProductionAssignment.objects.create(draft=second_draft, producer=self.producer, assigned_by=self.coordinator)
        self.assertEqual(self.select_issue(second_draft, issue).status_code, 400)
        second_draft.refresh_from_db()
        self.assertIsNone(second_draft.issue_id)

    def test_issue_pages_support_simplified_chinese(self):
        draft = self.ready()
        self.client.force_login(self.coordinator)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(reverse("manage_test_issues", args=[self.journal.slug])), "管理测试卷期")
        issue = self.create_issue()
        self.select_issue(draft, issue)
        self.client.force_login(self.author)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertContains(page, "测试期次")
        self.assertContains(page, "Fictional issue")

    def test_missing_published_pdf_blocks_issue_release(self):
        draft = self.ready()
        issue = self.create_issue()
        self.select_issue(draft, issue)
        self.approve(draft)
        self.publish(draft)
        proof = PublicationProof.objects.get(draft=draft)
        proof.file.storage.delete(proof.file.name)
        self.assertEqual(self.release_issue(issue).status_code, 409)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)
        self.assertEqual(Client().get(reverse("test_issue", args=[self.journal.slug, issue.pk])).status_code, 404)

    def test_issue_contents_follow_test_publication_order(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        first.refresh_from_db()
        second = Submission.objects.create(journal=self.journal, author=self.author, title="Earlier fictional manuscript",
            abstract="Fictional abstract", manuscript="manuscripts/earlier.pdf", status="accepted")
        version = SubmissionVersion.objects.create(submission=second, number=1, manuscript="manuscripts/earlier.pdf", uploaded_by=self.author)
        review_round = ReviewRound.objects.create(submission=second, version=version, number=1, status="closed")
        decision = EditorialDecision.objects.create(submission=second, round=review_round, outcome="accept", author_message="Accepted", decided_by=self.editor)
        earlier = PublicationDraft.objects.create(submission=second, decision=decision, issue=issue, title="Earlier test article",
            authors="Imaginary Author", abstract="Fictional", created_by=self.editor, published_by=self.producer,
            published_at=first.published_at-timedelta(minutes=1))
        proof = PublicationProof.objects.create(draft=earlier, file=SimpleUploadedFile("earlier.pdf", PDF),
                                                sha256=hashlib.sha256(PDF).hexdigest(), uploaded_by=self.producer)
        earlier.published_proof = proof
        earlier.save(update_fields=["published_proof"])
        self.assertEqual(self.release_issue(issue).status_code, 302)
        page = Client().get(reverse("test_issue", args=[self.journal.slug, issue.pk])).content.decode("utf-8")
        self.assertLess(page.index("Earlier test article"), page.index("Fictional public title"))

    def online_first(self):
        draft = self.ready()
        self.assertEqual(self.approve(draft).status_code, 302)
        self.assertEqual(self.publish(draft).status_code, 302)
        draft.refresh_from_db()
        return draft

    def issue_proposal_data(self, draft, issue):
        self.client.force_login(self.producer)
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertEqual(page.status_code, 200)
        pending = page.context["issue_proposal"]
        return {"issue":str(issue.pk), "revision":str(page.context["draft"].revision),
                "fingerprint":page.context["fingerprint"], "current_proposal":str(pending.pk) if pending else "",
                "proposal_token":page.context["issue_proposal_token"], "confirm":"yes", "synthetic":"yes"}

    def make_issue_proposal(self, draft, issue):
        data = self.issue_proposal_data(draft, issue)
        result = self.client.post(reverse("propose_published_issue", args=[draft.pk]), data)
        self.assertEqual(result.status_code, 302)
        return PublishedIssueProposal.objects.get(draft=draft, status="pending")

    def issue_decision_data(self, draft, actor, choice="accept"):
        self.client.force_login(actor)
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertEqual(page.status_code, 200)
        return {"choice":choice, "proposal_token":page.context["issue_proposal_token"], "confirm":"yes", "synthetic":"yes"}

    def test_online_first_issue_requires_author_confirmation_and_preserves_public_article(self):
        draft = self.online_first()
        issue = self.create_issue()
        original_approval = PublicationApproval.objects.get(draft=draft)
        original_proof, original_published = draft.published_proof_id, draft.published_at
        original_text = (draft.title, draft.authors, draft.abstract, draft.keywords_config)
        proposal = self.make_issue_proposal(draft, issue)
        draft.refresh_from_db()
        self.assertIsNone(draft.issue_id)
        public = Client()
        article_url = reverse("test_article", args=[self.journal.slug, draft.pk])
        pdf_url = reverse("test_article_pdf", args=[self.journal.slug, draft.pk])
        self.assertNotContains(public.get(article_url), "Volume 1")
        data = self.issue_decision_data(draft, self.author)
        url = reverse("resolve_published_issue", args=[draft.pk, proposal.pk])
        self.assertEqual(self.client.post(url, data).status_code, 302)
        draft.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual((draft.issue_id, proposal.status, proposal.resolved_by_id), (issue.pk, "accepted", self.author.pk))
        self.assertEqual((draft.published_proof_id, draft.published_at), (original_proof, original_published))
        self.assertEqual((draft.title, draft.authors, draft.abstract, draft.keywords_config), original_text)
        self.assertTrue(PublicationApproval.objects.filter(pk=original_approval.pk).exists())
        self.assertEqual(PublicationApproval.objects.filter(draft=draft, approved_by=self.author).count(), 2)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertContains(public.get(article_url), "Volume 1")
        self.assertEqual(b"".join(public.get(pdf_url).streaming_content), PDF)
        self.client.force_login(self.author)
        self.assertIsNotNone(self.client.get(reverse("production_detail", args=[draft.pk])).context["approval"])
        self.assertEqual(self.release_issue(issue).status_code, 302)
        self.assertContains(public.get(reverse("test_issue", args=[self.journal.slug, issue.pk])), "Fictional public title")
        self.assertEqual(self.select_issue(draft).status_code, 409)

    def test_online_first_issue_permissions_are_separate_for_producer_and_author(self):
        draft = self.online_first()
        issue = self.create_issue()
        data = self.issue_proposal_data(draft, issue)
        proposal_url = reverse("propose_published_issue", args=[draft.pk])
        for user in (self.author, self.editor, self.reviewer, self.coordinator, self.other_editor):
            self.client.force_login(user)
            self.assertEqual(self.client.post(proposal_url, data).status_code, 404)
        proposal = self.make_issue_proposal(draft, issue)
        decision = self.issue_decision_data(draft, self.author)
        url = reverse("resolve_published_issue", args=[draft.pk, proposal.pk])
        for user in (self.producer, self.editor, self.reviewer, self.coordinator, self.other_editor):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, decision).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url, {**decision, "synthetic":""}).status_code, 400)
        draft.refresh_from_db()
        self.assertIsNone(draft.issue_id)

    def test_online_first_issue_replacement_decline_and_stale_forms(self):
        draft = self.online_first()
        first, second = self.create_issue(), self.create_issue(number=2)
        old_data = self.issue_proposal_data(draft, first)
        url = reverse("propose_published_issue", args=[draft.pk])
        self.assertEqual(self.client.post(url, old_data).status_code, 302)
        old = PublishedIssueProposal.objects.get(draft=draft, status="pending")
        old_decision = self.issue_decision_data(draft, self.author)
        new_data = self.issue_proposal_data(draft, second)
        self.assertEqual(self.client.post(url, new_data).status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.status, "withdrawn")
        self.assertEqual(old.resolved_by, self.producer)
        self.assertEqual(self.client.post(url, new_data).status_code, 409)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("resolve_published_issue", args=[draft.pk, old.pk]), old_decision).status_code, 409)
        pending = PublishedIssueProposal.objects.get(draft=draft, status="pending")
        data = self.issue_decision_data(draft, self.author, choice="decline")
        self.assertEqual(self.client.post(reverse("resolve_published_issue", args=[draft.pk, pending.pk]), data).status_code, 302)
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.resolved_by), ("declined", self.author))
        draft.refresh_from_db()
        self.assertIsNone(draft.issue_id)
        pending = self.make_issue_proposal(draft, first)
        data = self.issue_decision_data(draft, self.producer, choice="withdraw")
        self.assertEqual(self.client.post(reverse("resolve_published_issue", args=[draft.pk, pending.pk]), data).status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "withdrawn")

    def test_online_first_issue_rejects_foreign_or_released_issues(self):
        draft = self.online_first()
        issue = self.create_issue()
        foreign = JournalIssue.objects.create(journal=self.other_journal, volume=1, number=1, year=2025, created_by=self.coordinator)
        data = self.issue_proposal_data(draft, foreign)
        url = reverse("propose_published_issue", args=[draft.pk])
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data["issue"] = str(issue.pk)
        JournalIssue.objects.filter(pk=issue.pk).update(released_at=timezone.now(), released_by=self.coordinator)
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertFalse(PublishedIssueProposal.objects.exists())

    def test_online_first_issue_revalidates_source_target_and_producer(self):
        draft = self.online_first()
        issue = self.create_issue()
        proposal = self.make_issue_proposal(draft, issue)
        data = self.issue_decision_data(draft, self.author)
        url = reverse("resolve_published_issue", args=[draft.pk, proposal.pk])
        JournalIssue.objects.filter(pk=issue.pk).update(released_at=timezone.now(), released_by=self.coordinator)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        JournalIssue.objects.filter(pk=issue.pk).update(released_at=None, released_by=None, title="Changed issue")
        self.assertEqual(self.client.post(url, data).status_code, 409)
        JournalIssue.objects.filter(pk=issue.pk).update(title=issue.title)
        Membership.objects.filter(user=self.producer, journal=self.journal, role="producer").update(active=False)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        Membership.objects.filter(user=self.producer, journal=self.journal, role="producer").update(active=True)
        PublicationDraft.objects.filter(pk=draft.pk).update(abstract="Changed after publication")
        self.assertEqual(self.client.post(url, data).status_code, 409)
        PublicationDraft.objects.filter(pk=draft.pk).update(abstract=draft.abstract)
        proof = draft.published_proof
        with open(proof.file.path, "ab") as output:
            output.write(b"tampered")
        self.assertEqual(self.client.post(url, data).status_code, 409)
        draft.refresh_from_db()
        proposal.refresh_from_db()
        self.assertIsNone(draft.issue_id)
        self.assertEqual(proposal.status, "pending")
        self.assertEqual(PublicationApproval.objects.filter(draft=draft).count(), 1)

    def test_online_first_issue_failure_rolls_back_all_changes(self):
        draft = self.online_first()
        issue = self.create_issue()
        proposal = self.make_issue_proposal(draft, issue)
        data = self.issue_decision_data(draft, self.author)
        with patch("publishing.published_issue_views.PublicationApproval.objects.create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("resolve_published_issue", args=[draft.pk, proposal.pk]), data)
        draft.refresh_from_db()
        proposal.refresh_from_db()
        self.assertIsNone(draft.issue_id)
        self.assertEqual(proposal.status, "pending")
        self.assertEqual(PublicationApproval.objects.filter(draft=draft).count(), 1)

    def test_online_first_issue_routes_require_csrf_and_local_feature(self):
        draft = self.online_first()
        issue = self.create_issue()
        data = self.issue_proposal_data(draft, issue)
        url = reverse("propose_published_issue", args=[draft.pk])
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.producer)
        self.assertEqual(guarded.post(url, data).status_code, 403)
        for settings_values in ({"IPS_LOCAL_PUBLICATION_ENABLED":False}, {"DEBUG":False}):
            with self.subTest(settings_values=settings_values), override_settings(**settings_values):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        proposal = self.make_issue_proposal(draft, issue)
        data = self.issue_decision_data(draft, self.author)
        guarded.force_login(self.author)
        self.assertEqual(guarded.post(reverse("resolve_published_issue", args=[draft.pk, proposal.pk]), data).status_code, 403)

    def test_online_first_issue_proposal_is_available_in_simplified_chinese(self):
        draft = self.online_first()
        issue = self.create_issue()
        self.make_issue_proposal(draft, issue)
        self.client.force_login(self.author)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("production_detail", args=[draft.pk]))
        self.assertContains(page, "将先行发表的测试文章编入期次")
        self.assertContains(page, "确认编入期次")
        self.assertContains(page, "Fictional issue")

    def extra_issue_article(self, issue, title, published_at):
        submission = Submission.objects.create(journal=self.journal, author=self.author, title=title,
            abstract="Fictional abstract", manuscript="manuscripts/example.pdf", status="accepted")
        version = SubmissionVersion.objects.create(submission=submission, number=1,
            manuscript="manuscripts/example.pdf", uploaded_by=self.author)
        round_ = ReviewRound.objects.create(submission=submission, version=version, number=1, status="closed")
        decision = EditorialDecision.objects.create(submission=submission, round=round_, outcome="accept",
            author_message="Fictional acceptance", decided_by=self.editor)
        draft = PublicationDraft.objects.create(submission=submission, decision=decision, issue=issue,
            title=title, authors="Fictional author", abstract="Fictional abstract", created_by=self.editor,
            published_by=self.producer, published_at=published_at)
        proof = PublicationProof.objects.create(draft=draft, file=SimpleUploadedFile("issue-test.pdf", PDF),
            sha256=hashlib.sha256(PDF).hexdigest(), uploaded_by=self.producer)
        draft.published_proof = proof
        draft.save(update_fields=["published_proof"])
        return draft

    def order_page(self, issue, actor=None):
        self.client.force_login(actor or self.coordinator)
        response = self.client.get(reverse("order_test_issue", args=[issue.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def order_data(self, page, desired):
        return {"state":page.context["order_state"], "synthetic":"yes",
                **{"position_" + str(article.pk):str(index) for index, article in enumerate(desired, 1)}}

    def test_issue_manual_order_is_published_and_fixed_on_release(self):
        first = self.ready()
        issue = self.create_issue()
        self.assertEqual(self.select_issue(first, issue).status_code, 302)
        self.assertEqual(self.approve(first).status_code, 302)
        self.assertEqual(self.publish(first).status_code, 302)
        first.refresh_from_db()
        second = self.extra_issue_article(issue, "Fictional second article", first.published_at + timedelta(minutes=1))
        third = self.extra_issue_article(issue, "Fictional third article", first.published_at + timedelta(minutes=2))
        page = self.order_page(issue)
        self.assertEqual([a.pk for a in page.context["articles"]], [first.pk, second.pk, third.pk])
        desired = [third, first, second]
        self.assertEqual(self.client.post(reverse("order_test_issue", args=[issue.pk]), self.order_data(page, desired)).status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.article_order, [str(a.pk) for a in desired])
        self.assertEqual(SubmissionEvent.objects.filter(action="test_issue_order_saved").count(), 3)
        self.assertEqual(self.release_issue(issue).status_code, 302)
        public = Client().get(reverse("test_issue", args=[self.journal.slug, issue.pk])).content.decode()
        self.assertLess(public.index(third.title), public.index(first.title))
        self.assertLess(public.index(first.title), public.index(second.title))
        for draft in desired:
            response = Client().get(reverse("test_article_pdf", args=[self.journal.slug, draft.pk]))
            self.assertEqual(b"".join(response.streaming_content), PDF)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse("order_test_issue", args=[issue.pk])).status_code, 409)
        self.assertEqual(self.client.post(reverse("order_test_issue", args=[issue.pk]), self.order_data(page, desired)).status_code, 409)

    def test_issue_order_rejects_stale_invalid_and_forged_positions(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        first.refresh_from_db()
        second = self.extra_issue_article(issue, "Second issue article", first.published_at + timedelta(minutes=1))
        page = self.order_page(issue)
        url = reverse("order_test_issue", args=[issue.pk])
        data = self.order_data(page, [first, second])
        for changes in ({"position_" + str(first.pk):"2"}, {"position_" + str(first.pk):"0"},
                        {"position_" + str(first.pk):"99999999999999999999"}, {"position_fake":"3"},
                        {"synthetic":""}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(url, {**data, **changes}).status_code, 400)
        missing = dict(data)
        missing.pop("position_" + str(first.pk))
        self.assertEqual(self.client.post(url, missing).status_code, 400)
        duplicated = {**data, "position_" + str(first.pk):["1", "1"]}
        self.assertEqual(self.client.post(url, duplicated).status_code, 400)
        self.assertEqual(self.client.post(url, self.order_data(page, [second, first])).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        issue.refresh_from_db()
        self.assertEqual(issue.article_order, [str(second.pk), str(first.pk)])

    def test_issue_order_appends_new_articles_and_rejects_old_forms(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        first.refresh_from_db()
        second = self.extra_issue_article(issue, "New second article", first.published_at + timedelta(minutes=1))
        page = self.order_page(issue)
        url = reverse("order_test_issue", args=[issue.pk])
        self.assertEqual(self.client.post(url, self.order_data(page, [second, first])).status_code, 302)
        stale = self.order_page(issue)
        third = self.extra_issue_article(issue, "New third article", first.published_at + timedelta(minutes=2))
        self.assertEqual(self.client.post(url, self.order_data(stale, [first, second])).status_code, 409)
        page = self.order_page(issue)
        self.assertEqual([a.pk for a in page.context["articles"]], [second.pk, first.pk, third.pk])
        self.assertEqual(self.release_issue(issue).status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.article_order, [str(second.pk), str(first.pk), str(third.pk)])

    def test_saving_default_issue_order_appends_earlier_online_first_articles(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        first.refresh_from_db()
        second = self.extra_issue_article(issue, "Default order second", first.published_at + timedelta(minutes=1))
        page = self.order_page(issue)
        url = reverse("order_test_issue", args=[issue.pk])
        self.assertEqual(self.client.post(url, self.order_data(page, [first, second])).status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.article_order, [str(first.pk), str(second.pk)])
        third = self.extra_issue_article(issue, "Older online-first third", first.published_at - timedelta(minutes=2))
        page = self.order_page(issue)
        self.assertEqual([a.pk for a in page.context["articles"]], [first.pk, second.pk, third.pk])
        self.assertEqual(self.client.post(url, self.order_data(page, [first, second, third])).status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.article_order, [str(first.pk), str(second.pk), str(third.pk)])
        fourth = self.extra_issue_article(issue, "Older online-first fourth", first.published_at - timedelta(minutes=3))
        self.assertEqual([a.pk for a in self.order_page(issue).context["articles"]], [first.pk, second.pk, third.pk, fourth.pk])

    def test_issue_order_requires_independent_coordinator(self):
        draft = self.ready()
        issue = self.create_issue()
        self.select_issue(draft, issue)
        self.approve(draft)
        self.publish(draft)
        url = reverse("order_test_issue", args=[issue.pk])
        for user in (self.author, self.reviewer, self.producer, self.editor):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        Membership.objects.create(user=self.author, journal=self.journal, role="coordinator")
        Membership.objects.create(user=self.reviewer, journal=self.journal, role="coordinator")
        for user in (self.author, self.reviewer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"synthetic":"yes"}).status_code, 403)

    def test_issue_order_blocks_unpublished_assignments_and_hides_their_titles(self):
        issue = self.create_issue()
        draft = self.start()
        self.select_issue(draft, issue)
        page = self.order_page(issue)
        self.assertContains(page, "Publish all assigned test articles")
        self.assertNotContains(page, "Fictional original title")
        url = reverse("order_test_issue", args=[issue.pk])
        self.assertEqual(self.client.post(url, {"state":page.context["order_state"], "synthetic":"yes"}).status_code, 409)
        self.assertEqual(self.release_issue(issue).status_code, 409)

    def test_issue_order_rejects_corrupt_stored_positions(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        JournalIssue.objects.filter(pk=issue.pk).update(article_order=[str(first.pk), str(first.pk)])
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse("order_test_issue", args=[issue.pk])).status_code, 409)
        self.assertEqual(self.release_issue(issue).status_code, 409)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)

    def test_issue_order_routes_require_csrf_and_fictional_feature(self):
        first = self.ready()
        issue = self.create_issue()
        self.select_issue(first, issue)
        self.approve(first)
        self.publish(first)
        page = self.order_page(issue)
        url = reverse("order_test_issue", args=[issue.pk])
        data = self.order_data(page, [first])
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.coordinator)
        self.assertEqual(guarded.post(url, data).status_code, 403)
        for setting in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with self.subTest(setting=setting), override_settings(**setting):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, data).status_code, 404)

    def test_issue_order_screen_supports_simplified_chinese(self):
        issue = self.create_issue()
        self.client.force_login(self.coordinator)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("order_test_issue", args=[issue.pk]))
        self.assertContains(page, "排列测试期次文章")
        self.assertContains(page, "此期次尚未分配已发表的测试文章")

    def release_review(self, issue):
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse("review_test_issue_release", args=[issue.pk]))
        self.assertEqual(page.status_code, 200)
        return page, {"release_token":page.context["release_token"], "confirm":"yes", "synthetic":"yes"}

    def test_issue_release_review_matches_public_order_without_writes(self):
        issue = self.create_issue()
        first = self.extra_issue_article(issue, "First test article", timezone.now())
        second = self.extra_issue_article(issue, "<script>Second test article</script>", timezone.now())
        order = self.order_page(issue)
        self.client.post(reverse("order_test_issue", args=[issue.pk]), self.order_data(order, [second, first]))
        before = list(PublicationDraft.objects.values())
        events = SubmissionEvent.objects.count()
        page, data = self.release_review(issue)
        self.assertEqual([a.pk for a in page.context["articles"]], [second.pk, first.pk])
        self.assertContains(page, "&lt;script&gt;Second test article&lt;/script&gt;")
        self.assertNotContains(page, "<script>Second test article</script>")
        self.assertNotContains(page, "SECRET fictional review")
        self.assertContains(page, reverse("test_article_pdf", args=[self.journal.slug, first.pk]))
        self.assertIn("no-store", page["Cache-Control"])
        self.assertIn("noindex", page["X-Robots-Tag"])
        self.assertEqual(list(PublicationDraft.objects.values()), before)
        self.assertEqual(SubmissionEvent.objects.count(), events)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)
        self.assertEqual(Client().get(reverse("test_issue", args=[self.journal.slug, issue.pk])).status_code, 404)
        listing = self.client.get(reverse("manage_test_issues", args=[self.journal.slug]))
        self.assertContains(listing, reverse("review_test_issue_release", args=[issue.pk]))
        self.assertNotContains(listing, 'action="' + reverse("release_test_issue", args=[issue.pk]) + '"')
        self.assertEqual(self.client.post(reverse("release_test_issue", args=[issue.pk]), data).status_code, 302)
        public = Client().get(reverse("test_issue", args=[self.journal.slug, issue.pk]))
        self.assertEqual([a.pk for a in public.context["articles"]], [second.pk, first.pk])
        self.assertContains(public, "Articles follow the order fixed")
        self.assertNotContains(public, "Articles are ordered by local test publication time.")
        self.assertEqual(list(PublicationDraft.objects.values()), before)

    def test_issue_release_rejects_missing_tampered_expired_and_replayed_review(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        _, data = self.release_review(issue)
        url = reverse("release_test_issue", args=[issue.pk])
        for token in ("", "invalid", data["release_token"] + "x"):
            self.assertEqual(self.client.post(url, {**data, "release_token":token}).status_code, 409)
        with patch("django.core.signing.time.time", return_value=time.time() + 3602):
            self.assertEqual(self.client.post(url, data).status_code, 409)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        issue.refresh_from_db()
        released_at = issue.released_at
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertEqual(self.client.get(reverse("review_test_issue_release", args=[issue.pk])).status_code, 409)
        issue.refresh_from_db()
        self.assertEqual(issue.released_at, released_at)

    def test_expired_issue_review_links_to_fresh_review_without_releasing(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        _, data = self.release_review(issue)
        url = reverse("release_test_issue", args=[issue.pk])
        review_url = reverse("review_test_issue_release", args=[issue.pk])
        for language in ("en", "zh-hans"):
            self.client.post(reverse("set_language"), {"language":language, "next":"/"})
            with patch("django.core.signing.time.time", return_value=time.time() + 3602):
                response = self.client.post(url, data)
            self.assertContains(response, f'href="{review_url}"', status_code=409)
            self.assertNotContains(response, 'name="release_token"', status_code=409)
            self.assertNotContains(response, 'name="confirm"', status_code=409)
            if language == "zh-hans":
                self.assertContains(response, "发布核对已过期或期次内容已更改", status_code=409)
                self.assertContains(response, "发布前核对", status_code=409)
            else:
                self.assertContains(response, "The release review expired or the issue changed.", status_code=409)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")
            issue.refresh_from_db()
            self.assertIsNone(issue.released_at)
            self.assertIsNone(issue.article_order)

        fresh = self.client.get(review_url)
        self.assertEqual(fresh.status_code, 200)
        self.assertTrue(fresh.context["release_token"])
        self.assertEqual(self.client.post(url, {**data, "release_token":fresh.context["release_token"]}).status_code, 302)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.released_at)

    def test_issue_release_review_binds_actor_issue_and_current_permissions(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        _, data = self.release_review(issue)
        second = self.create_issue(number=2)
        self.extra_issue_article(second, "Other test article", timezone.now())
        self.assertEqual(self.client.post(reverse("release_test_issue", args=[second.pk]), data).status_code, 409)
        peer = get_user_model().objects.create_user("other-issue-coordinator")
        Membership.objects.create(user=peer, journal=self.journal, role="coordinator")
        self.client.force_login(peer)
        self.assertEqual(self.client.post(reverse("release_test_issue", args=[issue.pk]), data).status_code, 409)
        self.client.force_login(self.coordinator)
        Membership.objects.filter(user=self.coordinator, journal=self.journal).update(active=False)
        self.assertEqual(self.client.post(reverse("release_test_issue", args=[issue.pk]), data).status_code, 404)
        self.assertEqual(self.client.get(reverse("review_test_issue_release", args=[issue.pk])).status_code, 404)
        issue.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(issue.released_at)
        self.assertIsNone(second.released_at)

    def test_issue_release_review_rejects_changed_metadata_and_order(self):
        issue = self.create_issue()
        first = self.extra_issue_article(issue, "First test article", timezone.now())
        second = self.extra_issue_article(issue, "Second test article", timezone.now())
        _, data = self.release_review(issue)
        url = reverse("release_test_issue", args=[issue.pk])
        cases = [(JournalIssue.objects.filter(pk=issue.pk), "title", issue.title, "Changed issue"),
                 (JournalIssue.objects.filter(pk=issue.pk), "year", issue.year, issue.year + 1),
                 (Journal.objects.filter(pk=self.journal.pk), "name", self.journal.name, "Changed journal"),
                 (PublicationDraft.objects.filter(pk=first.pk), "title", first.title, "Changed article"),
                 (PublicationDraft.objects.filter(pk=first.pk), "authors", first.authors, "Changed author"),
                 (PublicationDraft.objects.filter(pk=first.pk), "abstract", first.abstract, "Changed abstract"),
                 (PublicationDraft.objects.filter(pk=first.pk), "published_at", first.published_at, first.published_at - timedelta(days=1)),
                 (JournalIssue.objects.filter(pk=issue.pk), "article_order", None, [str(second.pk), str(first.pk)])]
        for queryset, field, original, changed in cases:
            with self.subTest(field=field, model=queryset.model.__name__):
                queryset.update(**{field:changed})
                self.assertEqual(self.client.post(url, data).status_code, 409)
                issue.refresh_from_db()
                self.assertIsNone(issue.released_at)
                queryset.update(**{field:original})
        self.assertEqual(self.client.post(url, data).status_code, 302)

    def test_issue_release_review_rejects_added_articles_and_allows_fresh_review(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "First test article", timezone.now())
        _, data = self.release_review(issue)
        self.extra_issue_article(issue, "New test article", timezone.now() - timedelta(days=1))
        url = reverse("release_test_issue", args=[issue.pk])
        self.assertEqual(self.client.post(url, data).status_code, 409)
        page, fresh = self.release_review(issue)
        self.assertEqual(len(page.context["articles"]), 2)
        self.assertEqual(self.client.post(url, fresh).status_code, 302)

    def test_issue_release_review_blocks_missing_or_replaced_pdf(self):
        issue = self.create_issue()
        article = self.extra_issue_article(issue, "Test article", timezone.now())
        _, data = self.release_review(issue)
        url = reverse("release_test_issue", args=[issue.pk])
        proof = article.published_proof
        proof.file.storage.delete(proof.file.name)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        page, blocked = self.release_review(issue)
        self.assertEqual(blocked["release_token"], "")
        self.assertContains(page, "Published PDF missing or changed")
        self.assertNotContains(page, 'name="release_token"')
        changed = PDF.replace(b"Fictional publication proof", b"Changed fictional proof")
        proof.file.save("changed-test.pdf", ContentFile(changed), save=False)
        proof.sha256 = hashlib.sha256(changed).hexdigest()
        proof.save(update_fields=["file", "sha256"])
        self.assertEqual(self.client.post(url, data).status_code, 409)
        _, fresh = self.release_review(issue)
        self.assertEqual(self.client.post(url, fresh).status_code, 302)

    def test_issue_release_review_hides_unpublished_and_foreign_drafts(self):
        issue = self.create_issue()
        draft = self.start()
        self.select_issue(draft, issue)
        page, data = self.release_review(issue)
        self.assertEqual(data["release_token"], "")
        self.assertNotContains(page, "Fictional original title")
        self.assertNotContains(page, 'name="release_token"')
        foreign = self.extra_issue_article(issue, "Foreign unpublished secret", timezone.now())
        Submission.objects.filter(pk=foreign.submission_id).update(journal=self.other_journal)
        page, data = self.release_review(issue)
        self.assertEqual(data["release_token"], "")
        self.assertNotContains(page, "Foreign unpublished secret")

    def test_issue_release_review_handles_empty_and_corrupt_issue(self):
        issue = self.create_issue()
        page, data = self.release_review(issue)
        self.assertEqual(data["release_token"], "")
        self.assertContains(page, "at least one published article")
        article = self.extra_issue_article(issue, "Test article", timezone.now())
        JournalIssue.objects.filter(pk=issue.pk).update(article_order=[str(article.pk), str(article.pk)])
        page, data = self.release_review(issue)
        self.assertEqual(data["release_token"], "")
        self.assertContains(page, "The saved issue order is invalid")

    def test_issue_release_review_enforces_scope_conflicts_and_feature_gates(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        url = reverse("review_test_issue_release", args=[issue.pk])
        self.assertEqual(Client().get(url).status_code, 302)
        for user in (self.author, self.producer, self.editor):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        Membership.objects.create(user=self.author, journal=self.journal, role="coordinator")
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 403)
        _, data = self.release_review(issue)
        for values in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with self.subTest(values=values), override_settings(**values):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(reverse("release_test_issue", args=[issue.pk]), data).status_code, 404)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.coordinator)
        self.assertEqual(guarded.post(reverse("release_test_issue", args=[issue.pk]), data).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        for field in ("confirm", "synthetic"):
            self.assertEqual(self.client.post(reverse("release_test_issue", args=[issue.pk]), {**data, field:""}).status_code, 400)

    def test_issue_release_failure_rolls_back_order_and_release(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        _, data = self.release_review(issue)
        original_save = JournalIssue.save
        def fail_after_save(instance, *args, **kwargs):
            original_save(instance, *args, **kwargs)
            raise RuntimeError("Simulated issue write failure")
        with patch.object(JournalIssue, "save", fail_after_save), self.assertRaises(RuntimeError):
            self.client.post(reverse("release_test_issue", args=[issue.pk]), data)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)
        self.assertIsNone(issue.released_by_id)
        self.assertIsNone(issue.article_order)

    def test_issue_release_review_supports_simplified_chinese(self):
        issue = self.create_issue()
        self.extra_issue_article(issue, "Test article", timezone.now())
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page, data = self.release_review(issue)
        self.assertContains(page, "测试期次发布前核对")
        self.assertContains(page, "按发布顺序排列的期次目录")
        self.assertContains(page, "确认发布期次")
        self.assertTrue(data["release_token"])
