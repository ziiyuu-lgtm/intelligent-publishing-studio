"""Run the fictional issue-release browser checks against a disposable test DB.

Requires the separate browser-test dependency and an installed Chromium build,
or installed Google Chrome with IPS_BROWSER_CHANNEL=chrome.
No existing server, account, database, email service or AI provider is used.
"""
import hashlib
import os
from pathlib import Path
import secrets
import sys
import tempfile
from datetime import timedelta
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings") != "config.settings":
    raise SystemExit("Browser checks require the local fictional-data settings.")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.test.utils import get_runner
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect, sync_playwright

from publishing.models import (EditorialDecision, Journal, JournalIssue, Membership,
    Organisation, PublicationDraft, PublicationProof, ReviewRound, Submission, SubmissionVersion)

PDF = b"%PDF-1.4\nFictional browser-check proof\n%%EOF\n"
ARTIFACTS = Path(os.environ.get("IPS_BROWSER_ARTIFACT_DIR", ROOT / "artifacts" / "issue-release-browser"))


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
    IPS_OPENAI_ENABLED=False, IPS_OPENAI_IMAGE_ENABLED=False,
    IPS_LOCAL_LOGIN_THROTTLE_ENABLED=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IssueReleaseBrowserChecks(StaticLiveServerTestCase):
    host = "127.0.0.1"

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="ips-browser-proof-")
        cls.addClassCleanup(temporary.cleanup)
        media = override_settings(MEDIA_ROOT=temporary.name)
        media.enable()
        cls.addClassCleanup(media.disable)
        super().setUpClass()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        users = get_user_model().objects
        self.password = secrets.token_urlsafe(24)
        self.coordinator = users.create_user("test_browser_coordinator", password=self.password)
        self.author = users.create_user("test_browser_author")
        self.editor = users.create_user("test_browser_editor")
        self.organisation = Organisation.objects.create(name="Fictional Test Publisher", slug="browser-publisher")
        self.journal = Journal.objects.create(organisation=self.organisation,
            name="Sample Journal A (fictional test)", slug="browser-journal")
        Membership.objects.create(user=self.coordinator, journal=self.journal, role="coordinator")
        self.issue = self.new_issue(99)
        titles = ["A Demonstration Workflow for Digital Publishing Systems",
                  "Reviewing Publication Metadata Across Journal Workflows",
                  "Maintaining Article Order in an Online-First Test Issue"]
        self.articles = [self.new_article(self.issue, title, index) for index, title in enumerate(titles, 1)]
        # Deliberately differs from publication-time order.
        self.issue.article_order = [str(article.pk) for article in self.articles]
        self.issue.save(update_fields=["article_order"])

    @contextmanager
    def browser_session(self):
        # The sync browser API runs an event loop in this thread. Perform ORM
        # setup/assertions outside this scope; the live server has its own thread.
        with sync_playwright() as playwright:
            channel = os.environ.get("IPS_BROWSER_CHANNEL") or None
            if channel not in (None, "chrome"):
                raise ValueError("Use the bundled Chromium build or IPS_BROWSER_CHANNEL=chrome.")
            browser = playwright.chromium.launch(headless=True, channel=channel)
            self.context = browser.new_context(viewport={"width": 1440, "height": 1000})
            try:
                self.page = self.context.new_page()
                self.page_errors = []
                self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
                self.page.goto(self.url("manage_test_issues", self.journal.slug))
                self.page.get_by_label("Username", exact=True).fill(self.coordinator.username)
                self.page.get_by_label("Password", exact=True).fill(self.password)
                self.page.get_by_role("button", name="Sign in", exact=True).click()
                expect(self.page.get_by_role("heading", name="Manage test issues", exact=True)).to_be_visible()
                yield
            finally:
                self.context.close()
                browser.close()

    def url(self, name, *args):
        return self.live_server_url + reverse(name, args=args)

    def new_issue(self, number):
        return JournalIssue.objects.create(journal=self.journal, volume=1, number=number,
            year=2026, title="Fictional browser acceptance", created_by=self.coordinator)

    def new_article(self, issue, title, index, published=True):
        submission = Submission.objects.create(journal=self.journal, author=self.author,
            title=title, abstract="Fictional browser-check abstract.", manuscript="fictional.pdf", status="accepted")
        version = SubmissionVersion.objects.create(submission=submission, number=1,
            manuscript="fictional.pdf", uploaded_by=self.author)
        review_round = ReviewRound.objects.create(submission=submission, version=version, number=1, status="closed")
        decision = EditorialDecision.objects.create(submission=submission, round=review_round,
            outcome="accept", author_message="Fictional acceptance.", decided_by=self.editor)
        article = PublicationDraft.objects.create(submission=submission, decision=decision, issue=issue,
            title=title, authors="Demo Author; Example Author", abstract="Fictional public abstract.",
            created_by=self.editor, published_by=self.editor if published else None,
            published_at=timezone.now() - timedelta(days=index) if published else None)
        if published:
            proof = PublicationProof.objects.create(draft=article,
                file=SimpleUploadedFile("fictional-proof.pdf", PDF),
                sha256=hashlib.sha256(PDF).hexdigest(), uploaded_by=self.editor)
            article.published_proof = proof
            article.save(update_fields=["published_proof"])
        return article

    def assert_order(self, articles):
        headings = self.page.locator("ol > li h3")
        expect(headings).to_have_count(len(articles))
        self.assertEqual(headings.all_text_contents(), [article.title for article in articles])

    def screenshot(self, filename):
        self.page.screenshot(path=str(ARTIFACTS / filename), full_page=True)

    def assert_no_horizontal_overflow(self):
        widths = self.page.evaluate("({page: document.documentElement.scrollWidth, viewport: window.innerWidth})")
        self.assertLessEqual(widths["page"], widths["viewport"] + 1)

    def test_review_reorder_stale_form_and_bilingual_release(self):
        desired = [self.articles[2], self.articles[0], self.articles[1]]
        with self.browser_session():
            expect(self.page.locator('input[name="confirm"]')).to_have_count(0)
            self.page.get_by_role("link", name="Review before release", exact=True).click()
            expect(self.page.get_by_role("heading", name="Review test issue release", exact=True)).to_be_visible()
            self.assert_order(self.articles)
            self.assertEqual(self.context.request.get(self.url("test_issue", self.journal.slug, self.issue.pk)).status, 404)
            self.assert_no_horizontal_overflow()
            self.screenshot("review-en-desktop.png")
            with self.page.expect_download() as download_info:
                self.page.get_by_role("link", name="Download published PDF", exact=True).first.click()
            self.assertEqual(Path(download_info.value.path()).read_bytes(), PDF)

            # Keep the first review open while another tab changes the order.
            arrange = self.context.new_page()
            arrange.goto(self.url("order_test_issue", self.issue.pk))
            for position, article in enumerate(desired, 1):
                arrange.get_by_label(article.title, exact=True).fill(str(position))
            arrange.locator('input[name="synthetic"]').check()
            arrange.get_by_role("button", name="Save article order", exact=True).click()
            expect(arrange.get_by_text("Test issue order saved.", exact=True)).to_be_visible()
            arrange.close()
            self.page.locator('input[name="confirm"]').check()
            self.page.locator('input[name="synthetic"]').check()
            release_url = self.url("release_test_issue", self.issue.pk)
            with self.page.expect_response(lambda response: response.url == release_url) as rejected:
                self.page.get_by_role("button", name="Release issue", exact=True).click()
            self.assertEqual(rejected.value.status, 409)
            expect(self.page.get_by_text("The release review expired or the issue changed.", exact=False)).to_be_visible()
            self.assertEqual(self.context.request.get(self.url("test_issue", self.journal.slug, self.issue.pk)).status, 404)

            expect(self.page.get_by_role("button", name="Release issue", exact=True)).to_have_count(0)
            self.page.get_by_role("link", name="Review before release", exact=True).click()
            self.assert_order(desired)
            self.page.get_by_label("Language", exact=True).select_option("zh-hans")
            expect(self.page.get_by_role("heading", name="测试期次发布前核对", exact=True)).to_be_visible()
            self.screenshot("review-zh-desktop.png")
            self.page.set_viewport_size({"width": 390, "height": 844})
            self.assert_no_horizontal_overflow()
            self.screenshot("review-zh-mobile.png")
            self.page.locator('input[name="confirm"]').check()
            self.page.locator('input[name="synthetic"]').check()
            self.page.get_by_role("button", name="发布期次", exact=True).click()
            expect(self.page.get_by_role("link", name="查看期次", exact=True)).to_be_visible()
            self.page.get_by_role("link", name="查看期次", exact=True).click()
            self.assertEqual(self.page.locator("section .list-row strong").all_text_contents(), [a.title for a in desired])
            expect(self.page.get_by_text("文章按此测试期次发布时固定的顺序排列。", exact=True)).to_be_visible()
            self.assert_no_horizontal_overflow()
            self.screenshot("released-zh-mobile.png")
            response = self.page.goto(self.url("review_test_issue_release", self.issue.pk))
            self.assertEqual(response.status, 409)
            self.assertEqual(self.page_errors, [])
        self.issue.refresh_from_db()
        self.assertIsNotNone(self.issue.released_at)
        self.assertEqual(self.issue.article_order, [str(a.pk) for a in desired])

    def test_blocked_issues_have_no_release_form_or_private_draft_titles(self):
        empty = self.new_issue(100)
        incomplete = self.new_issue(101)
        self.new_article(incomplete, "PRIVATE UNPUBLISHED FICTIONAL TITLE", 1, published=False)
        damaged = self.new_issue(102)
        article = self.new_article(damaged, "Fictional article with a missing test PDF", 1)
        proof = article.published_proof
        proof.file.storage.delete(proof.file.name)
        with self.browser_session():
            self.page.goto(self.url("review_test_issue_release", empty.pk))
            expect(self.page.get_by_text("The issue needs at least one published article", exact=False)).to_be_visible()
            expect(self.page.get_by_role("button", name="Release issue", exact=True)).to_have_count(0)
            self.page.goto(self.url("review_test_issue_release", incomplete.pk))
            expect(self.page.get_by_role("button", name="Release issue", exact=True)).to_have_count(0)
            expect(self.page.get_by_text("PRIVATE UNPUBLISHED FICTIONAL TITLE", exact=True)).to_have_count(0)
            self.page.goto(self.url("review_test_issue_release", damaged.pk))
            expect(self.page.get_by_text("Published PDF missing or changed", exact=True)).to_be_visible()
            expect(self.page.get_by_role("button", name="Release issue", exact=True)).to_have_count(0)
            expect(self.page.get_by_role("link", name="Download published PDF", exact=True)).to_have_count(0)
            self.screenshot("blocked-pdf-en-desktop.png")
            self.assertEqual(self.page_errors, [])
        damaged.refresh_from_db()
        self.assertIsNone(damaged.released_at)


if __name__ == "__main__":
    runner = get_runner(settings)(verbosity=2, interactive=False)
    raise SystemExit(bool(runner.run_tests(["__main__.IssueReleaseBrowserChecks"])))
