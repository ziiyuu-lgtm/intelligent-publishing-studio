# Local multi-journal workflow (development only)

This separate Django application is an early backend for testing the publishing workflow with entirely fictional data. It does not serve or modify the static prototype in `dist/` and is not deployed to SCIENTARA. It must not be used for real manuscripts or exposed to the public internet.

## What works locally

- An organisation can contain multiple journals. Test users can have author, editor or reviewer roles in individual journals; the same user may have multiple roles.
- An author with a journal role can create a draft with a PDF, then explicitly submit it. Editors cannot see drafts or perform editorial actions on their own submissions.
- An authorised editor can record a reviewer invitation for the same journal. The assigned reviewer can accept or decline, and can download the test file only after acceptance. An accepted reviewer can submit one confidential report; the editor can read it. Reviewer identities and reports are not displayed to authors.
- Authentication uses Django accounts and sessions; state changes use POST and CSRF protection. Role checks are enforced in server-side views. Privileged administrators do not automatically receive manuscript access through these views. A minimal event trail records the actor and action without storing manuscript or report text.
- PDF files are stored in `.runtime/private-media`, outside the static site, and downloaded only via an authorisation check. The development database is `.runtime/dev.sqlite3`. Both locations are ignored by Git. File checks limit the extension, basic header and size; they are not malware or content validation.
- The backend interface supports English and Simplified Chinese. Its separate static prototype retains the 12-language selector described in the root README.

## Run locally

Requires Python 3.10–3.14 and Node.js 18+ for the separate static-prototype tests. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000
```

On Windows, activate with `.venv\Scripts\activate` instead. Open <http://127.0.0.1:8000/>. The seed command creates a fictional SCIENTARA-inspired group, two fictional test journals and four randomly named test accounts. It prints unique, randomly generated passwords once in the terminal. Keep those credentials private. Sign in as the test author, editor and reviewer in separate browser sessions to try each role. Running the command again creates a separate set of test records; it does not reset existing accounts.

For manual management of test organisations, journals, users and journal roles, run `python manage.py createsuperuser` and visit `/admin/`. The admin site does not register submission and review models; having an administrator account does not itself grant manuscript access in the workspace. A development secret is generated at each process start, so sessions end when the server restarts.

## Validation

From `backend/`:

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test publishing.tests
```

From the repository root, run `node --test tests/*.test.cjs` and `python3 scripts/sync-locales.py --check` for the unchanged static interface. The compiled Chinese message catalogue is committed; after changing `locale/zh_Hans/LC_MESSAGES/django.po`, regenerate it with `python manage.py compilemessages -l zh_Hans` (GNU gettext is required).

## Limits and before real use

This is an intentionally incomplete local test implementation, not a controlled pilot or production service. It has no self-service account registration, password recovery, multifactor authentication, login throttling, mail delivery, conflict-of-interest declarations, double-blind anonymisation, reviewer deadlines, file revision history, editorial decisions, publication, DOI, AI integrations, backups, encryption at rest, virus scanning or production monitoring. A reviewer can read the title and abstract before accepting; the system does not claim to anonymise those fields or PDF contents. An author retains access to their own submission while the journal and account remain active, even if their author role is removed. A reviewer's journal role must remain active to access assigned work.

The SQLite database and development server are unsuitable for concurrent production use. Before any real-data pilot, replace the development settings with a separately designed and security-reviewed deployment, durable transactional database, TLS, secret management, access and retention policies, upload limits at the reverse proxy, malware scanning, safe storage and download controls, audit review, backups and tested restoration. Review all threat and privacy requirements, including journal-specific editorial assignments and conflicts. Do not assume that passing the included tests proves security or publishing compliance.
