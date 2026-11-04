"""Isolated PostgreSQL recovery rehearsal with fictional records and files.

This is a test, not a backup command for an operating journal. It only runs in
the dedicated local PostgreSQL probe profile with an explicit opt-in. The
randomly named databases and private temporary files are retained for review.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from publishing.models import Journal, Organisation, Submission, SubmissionVersion


class BackupDrillSafetyError(RuntimeError):
    pass


def validate_drill_context(database, environment, current_uid, postgres_uid):
    """Refuse any source other than the dedicated fictional test database."""
    base = environment.get("ALTAVIEN_PROBE_DB", "")
    if not re.fullmatch(r"altavien_probe_[0-9a-f]{24}", base):
        raise BackupDrillSafetyError("Invalid synthetic probe database.")
    if (environment.get("DJANGO_SETTINGS_MODULE") != "config.settings_postgres_probe"
            or environment.get("ALTAVIEN_PROBE_CONFIRM_SYNTHETIC") != "1"
            or environment.get("ALTAVIEN_BACKUP_DRILL_ENABLED") != "1"
            or current_uid != postgres_uid
            or database.get("ENGINE") != "django.db.backends.postgresql"
            or database.get("NAME") != base + "_test"
            or database.get("HOST") != "/var/run/postgresql"
            or str(database.get("PORT")) != "5432"
            or database.get("USER") != "postgres"
            or database.get("PASSWORD")
            or database.get("OPTIONS")):
        raise BackupDrillSafetyError("Unsafe context for the fictional backup drill.")
    return database["NAME"]


class BackupDrillContextTests(SimpleTestCase):
    def setUp(self):
        self.base = "altavien_probe_" + "1a" * 12
        self.environment = {
            "DJANGO_SETTINGS_MODULE": "config.settings_postgres_probe",
            "ALTAVIEN_PROBE_CONFIRM_SYNTHETIC": "1",
            "ALTAVIEN_BACKUP_DRILL_ENABLED": "1",
            "ALTAVIEN_PROBE_DB": self.base,
        }
        self.database = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": self.base + "_test", "HOST": "/var/run/postgresql",
            "PORT": "5432", "USER": "postgres", "PASSWORD": "", "OPTIONS": {},
        }

    def test_only_the_isolated_probe_database_and_local_postgres_account_are_accepted(self):
        validate_drill_context(self.database, self.environment, 42, 42)
        bad_databases = [
            {"NAME": "postgres"}, {"NAME": self.base},
            {"NAME": "altavien_probe_" + "2b" * 12 + "_test"},
            {"HOST": "127.0.0.1"}, {"HOST": "/tmp"}, {"PORT": "5433"},
            {"USER": "other"}, {"ENGINE": "django.db.backends.sqlite3"},
            {"PASSWORD": "not-allowed"}, {"OPTIONS": {"service": "other"}},
        ]
        for changes in bad_databases:
            with self.subTest(changes=changes), self.assertRaises(BackupDrillSafetyError):
                validate_drill_context(dict(self.database, **changes), self.environment, 42, 42)
        bad_environment = [
            {"ALTAVIEN_PROBE_DB": "postgres"}, {"ALTAVIEN_PROBE_CONFIRM_SYNTHETIC": "0"},
            {"ALTAVIEN_BACKUP_DRILL_ENABLED": "0"}, {"DJANGO_SETTINGS_MODULE": "config.settings"},
        ]
        for changes in bad_environment:
            with self.subTest(changes=changes), self.assertRaises(BackupDrillSafetyError):
                validate_drill_context(self.database, dict(self.environment, **changes), 42, 42)
        with self.assertRaises(BackupDrillSafetyError):
            validate_drill_context(self.database, self.environment, 41, 42)

    def test_explicit_opt_in_with_the_regular_database_fails_instead_of_skipping(self):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "ALTAVIEN_BACKUP_DRILL_ENABLED": "1",
            "IPS_REVIEW_AI_FINGERPRINT_KEY": "fictional-backup-test-key-with-at-least-32-characters",
        }
        result = subprocess.run(
            [sys.executable, "manage.py", "test",
             "publishing.tests.test_postgres_backup_drill.PostgreSQLBackupDrillTests", "--noinput", "-v", "1"],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("skipped=1", result.stdout + result.stderr)


class BackupDrillIntegrityTests(SimpleTestCase):
    def test_a_changed_archive_or_media_file_is_detected_before_restoration(self):
        with tempfile.TemporaryDirectory(prefix="altavien-fake-integrity-") as directory:
            root = Path(directory)
            archive, media = root / "database.dump", root / "fictional.pdf"
            _write_private_file(archive, b"fictional-database-dump")
            _write_private_file(media, b"fictional-manuscript")
            manifest = {"kind": "fictional-backup-drill-v1", "database_sha256": _sha256(archive),
                        "media_path": "manuscripts/fictional.pdf", "media_sha256": _sha256(media)}
            _verify_backup(archive, media, manifest, "manuscripts/fictional.pdf")
            media.write_bytes(b"corrupted-manuscript")
            with self.assertRaises(BackupDrillSafetyError):
                _verify_backup(archive, media, manifest, "manuscripts/fictional.pdf")
            media.write_bytes(b"fictional-manuscript")
            archive.write_bytes(b"corrupted-dump")
            with self.assertRaises(BackupDrillSafetyError):
                _verify_backup(archive, media, manifest, "manuscripts/fictional.pdf")


# Once explicitly enabled, an unsafe or unsupported environment must fail,
# rather than silently skipping the drill and reporting success.
_DRILL_ENABLED = os.environ.get("ALTAVIEN_BACKUP_DRILL_ENABLED") == "1"


@skipUnless(_DRILL_ENABLED, "Dedicated fictional PostgreSQL backup drill only")
class PostgreSQLBackupDrillTests(TransactionTestCase):
    """Dump a synthetic application DB and recover a record and its file."""

    def test_fictional_postgres_backup_restores_record_and_matching_private_file(self):
        import pwd

        try:
            postgres_uid = pwd.getpwnam("postgres").pw_uid
        except KeyError as exc:
            raise BackupDrillSafetyError("The local postgres account is unavailable.") from exc
        source_database = validate_drill_context(
            connection.settings_dict, os.environ, os.geteuid(), postgres_uid,
        )
        target_database = "altavien_restore_probe_" + secrets.token_hex(12)
        if not re.fullmatch(r"altavien_restore_probe_[0-9a-f]{24}", target_database):
            raise BackupDrillSafetyError("Invalid isolated restore database name.")

        root = Path(tempfile.mkdtemp(prefix="altavien-fictional-backup-"))
        source_media = root / "source-media"
        backup = root / "backup"
        restored_media = root / "restored-media"
        for directory in (source_media / "manuscripts", backup / "media" / "manuscripts",
                          restored_media / "manuscripts"):
            directory.mkdir(parents=True, mode=0o700)
        print(f"FICTIONAL_BACKUP_DIRECTORY: {root}", flush=True)

        relative_file = "manuscripts/fictional-recovery.pdf"
        fictional_pdf = b"%PDF-1.4\n% Fictional recovery drill only.\n%%EOF\n"
        source_file = source_media / relative_file
        _write_private_file(source_file, fictional_pdf)

        author = get_user_model().objects.create_user("fictional-backup-author")
        organisation = Organisation.objects.create(name="Fictional Backup Publisher", slug="fictional-backup-publisher")
        journal = Journal.objects.create(organisation=organisation, name="Fictional Backup Journal", slug="fictional-backup-journal")
        submission = Submission.objects.create(
            author=author, journal=journal, title="Fictional recovery manuscript",
            abstract="Invented data for the backup and restore drill.",
            manuscript=relative_file, status=Submission.Status.SUBMITTED,
        )
        SubmissionVersion.objects.create(submission=submission, number=1, manuscript=relative_file, uploaded_by=author)

        archive = backup / "database.dump"
        _write_private_file(archive, b"")
        _run_pg(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-privileges",
                 "--file", str(archive), "--dbname", source_database])
        backed_up_file = backup / "media" / relative_file
        _copy_private_file(source_file, backed_up_file)
        manifest = {
            "kind": "fictional-backup-drill-v1",
            "database_sha256": _sha256(archive),
            "media_path": relative_file,
            "media_sha256": _sha256(backed_up_file),
        }
        manifest_file = backup / "manifest.json"
        _write_private_file(manifest_file, json.dumps(manifest, sort_keys=True).encode("utf-8"))

        loaded = json.loads(manifest_file.read_text(encoding="utf-8"))
        _verify_backup(archive, backed_up_file, loaded, relative_file)

        # The name is random and created from template0. No existing database is
        # dropped, overwritten or used as a restore target, even on failure.
        _run_pg(["createdb", "--no-password", "--template=template0", "--maintenance-db=postgres", target_database])
        print(f"FICTIONAL_RESTORE_DATABASE: {target_database}", flush=True)
        _run_pg(["pg_restore", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner",
                 "--no-privileges", "--dbname", target_database, str(archive)])
        restored_file = restored_media / relative_file
        _copy_private_file(backed_up_file, restored_file)

        # The UUID was generated by the test fixture; it is not user input.
        query = (
            "SELECT json_build_object('title', s.title, 'path', s.manuscript, "
            "'journal', j.name, 'author', u.username, 'version', v.number, 'version_path', v.manuscript) "
            "FROM publishing_submission s JOIN publishing_journal j ON j.id=s.journal_id "
            "JOIN auth_user u ON u.id=s.author_id JOIN publishing_submissionversion v ON v.submission_id=s.id "
            f"WHERE s.id='{submission.pk}'::uuid;"
        )
        output = _run_pg(["psql", "--no-password", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align",
                          "--set", "ON_ERROR_STOP=1", "--dbname", target_database, "--command", query])
        restored = json.loads(output.strip())
        self.assertEqual(restored, {
            "title": "Fictional recovery manuscript", "path": relative_file,
            "journal": "Fictional Backup Journal", "author": "fictional-backup-author",
            "version": 1, "version_path": relative_file,
        })
        self.assertEqual(_sha256(restored_file), loaded["media_sha256"])
        self.assertEqual(restored_file.read_bytes(), fictional_pdf)
        self.assertEqual(root.stat().st_mode & 0o077, 0)
        self.assertEqual(archive.stat().st_mode & 0o077, 0)
        self.assertEqual(manifest_file.stat().st_mode & 0o077, 0)
        self.assertEqual(backed_up_file.stat().st_mode & 0o077, 0)
        self.assertEqual(restored_file.stat().st_mode & 0o077, 0)


def _write_private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _copy_private_file(source, destination):
    with source.open("rb") as src:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_backup(archive, media, manifest, expected_path):
    if (manifest.get("kind") != "fictional-backup-drill-v1"
            or manifest.get("media_path") != expected_path
            or manifest.get("database_sha256") != _sha256(archive)
            or manifest.get("media_sha256") != _sha256(media)):
        raise BackupDrillSafetyError("Fictional backup integrity check failed.")


def _run_pg(arguments):
    # Do not inherit libpq settings, credentials, service aliases or startup files.
    environment = {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C", "PGHOST": "/var/run/postgresql",
        "PGPORT": "5432", "PGUSER": "postgres", "PGDATABASE": "postgres",
        "PGPASSFILE": "/dev/null", "PGSERVICEFILE": "/dev/null", "PGCONNECT_TIMEOUT": "5",
    }
    completed = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, check=False, timeout=120)
    if completed.returncode:
        # This test operates only on invented data and contains no credentials.
        detail = completed.stderr.strip()[-2000:]
        raise BackupDrillSafetyError(f"{arguments[0]} failed ({completed.returncode}): {detail}")
    return completed.stdout
