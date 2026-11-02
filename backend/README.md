# Local multi-journal workflow (development only)

This separate Django application is an early backend for testing the publishing workflow with entirely fictional data. It does not serve or modify the static prototype in `dist/` and is not deployed to SCIENTARA. It must not be used for real manuscripts or exposed to the public internet.

## What works locally

- An organisation can contain multiple journals. Test users can have author, editor, reviewer, editorial coordinator or production editor roles in individual journals; the same user may have multiple roles.
- An author with a journal role can create a draft with a PDF, then explicitly submit it. Editors cannot see drafts or perform editorial actions on their own submissions.
- A journal coordinator assigns specific eligible editors after submission. The coordinator can see submission titles and assignment history, but receives no manuscript, abstract or confidential-review access solely from that role. Editors can access only assigned submissions while their journal role and assignment remain active. A coordinator cannot manage their own submission or one they reviewed or recused from; an author, invited reviewer or the coordinator making the assignment cannot be assigned as editor. A coordinator who is also an editor requires another eligible coordinator to assign them. Editors may recuse themselves, immediately ending their access and requiring a coordinator to arrange another editor. The assignment and recusal history is retained; an earlier decision is not automatically reversed. These checks do not establish the absence of other conflicts.
- Journals can use a standard review file or require a separate reviewer PDF; newly created journals default to the separate-file policy. The policy is captured when a submission is created. In separate-file mode, the author uploads and confirms a reviewer file for every version, and an authorised editor must manually inspect and record a check for that version before inviting reviewers. The reviewer receives the separate file only; there is no fallback to the author file if it is missing. Neither the PDF nor the title, abstract or metadata is anonymised automatically: both author and editor must inspect them, and the check cannot guarantee anonymity.
- An authorised editor can record a reviewer invitation for the same journal. Before accepting, the reviewer must explicitly declare no conflict. The reviewer may instead report a conflict with a private explanation, including if one arises after accepting or submitting a report. Conflict declarations are retained as a history and shown to authorised editors, not authors; access to the review file is blocked after a conflict is reported. The editor must assess the disclosure; earlier decisions are not automatically reversed. There is no email notification.
- The assigned reviewer can accept or decline and can download only the file for that open review round after acceptance and a no-conflict declaration. An accepted reviewer can submit one confidential report per round. Authors do not automatically receive reports or reviewer identities.
- An editor can record a decision and a separate message to the author. Acceptance or a revision request requires at least one completed report with a current no-conflict declaration; desk rejection is allowed. The decision closes the round, preventing unfinished invitations and reports from being completed. The message is visible to the author, so the editor is responsible for keeping reviewer identities and confidential text out of it.
- Following a revision request, the author uploads a new PDF and response to the editor. The prior file, decision and reports remain recorded, and a new review round opens for the new version. Reviewers need a new invitation and acceptance for that round; previous access does not carry over. Former reviewers cannot act as editors on that manuscript, and recorded editors cannot be invited to review it in a later round.
- Authentication uses Django accounts and sessions; state changes use POST and CSRF protection. Role checks are enforced in server-side views. Privileged administrators do not automatically receive manuscript access through these views. A minimal event trail records the actor and action without storing manuscript or report text.
- PDF files are stored in `.runtime/private-media`, outside the static site, and downloaded only via an authorisation check. The development database is `.runtime/dev.sqlite3`. Both locations are ignored by Git. File checks limit the extension, basic header and size; they are not malware or content validation.
- The backend interface supports English and Simplified Chinese. Its separate static prototype retains the 12-language selector described in the root README.
- Local rule-based checks report whether required fields and test files are present. They do not use AI, verify scientific quality or establish that a file is safe.
- A default-off local publication simulation requires an assigned editor to start production and an eligible coordinator to assign a production editor for that manuscript. An assigned production editor can access the accepted source PDF and production files but cannot access confidential reviews or reviewer identities merely through the production role. They prepare public metadata and a separate fictional publication PDF. The submitting author confirms the exact metadata and proof; the assigned production editor can then explicitly publish a local test article. Metadata edits and new proofs require fresh confirmation. Public test pages are marked `noindex` and show only the chosen test metadata and proof, not reviews. Anyone who can access the server can access a published test article.

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

On Windows, use `python` if `python3` is unavailable and activate with `.venv\Scripts\activate`. Open <http://127.0.0.1:8000/>. The seed command creates a fictional SCIENTARA-inspired group, two fictional test journals and six randomly named test accounts. It prints unique, randomly generated passwords once in the terminal. Keep those credentials private. Sign in as the test author, coordinator, editor, reviewer and production editor in separate browser sessions to try each role. After the author submits, the coordinator opens **Manage editor assignments** and assigns the test editor. The test journals require two fictional PDF files; the editor must inspect the reviewer copy before inviting the reviewer. Running the command again creates a separate set of test records; it does not reset existing accounts.

For manual management of test organisations, journals, users and journal roles, run `python manage.py createsuperuser` and visit `/admin/`. The admin site does not register submission and review models; having an administrator account does not itself grant manuscript access in the workspace. A development secret is generated at each process start, so sessions end when the server restarts.

The upgrade from the initial local backend creates version and round records for existing test submissions and retains the original file paths and review assignments. Existing journals and submissions retain the standard-file policy; changing a journal's policy affects new submissions only. Older accepted or submitted review tasks have an unknown conflict declaration rather than a fabricated one. Their reviewer must record a no-conflict declaration before file access or a further report, and an undeclared completed report does not count towards an acceptance or revision decision. Migration 0005 does not infer editorial assignments: existing submissions remain unassigned until an administrator grants a journal coordinator role and the coordinator explicitly assigns eligible editors. Migration 0006 does not create or publish articles automatically. Migration 0007 does not infer production assignments: a coordinator must explicitly assign an eligible production editor to each existing production draft before staff can access it. The migrations are deliberately one-way so that a rollback cannot silently discard new versions, declarations, assignments, proofs or decisions. Back up the test database and files before upgrading; restoring a backup is the supported way to return to the earlier test version.

## Optional local publication simulation

Use fictional materials only. The feature is disabled by default and works only with the development `DEBUG` setting. To enable it while binding the development server to the local machine, run from `backend/`:

```sh
IPS_LOCAL_PUBLICATION_ENABLED=1 python manage.py runserver 127.0.0.1:8000
```

In Windows PowerShell, set `$env:IPS_LOCAL_PUBLICATION_ENABLED='1'` before running `python manage.py runserver 127.0.0.1:8000`. To disable it, stop the server, unset the variable, and restart. A `noindex` header is a request to search engines, not an access control; any person who can connect to the server can view published test pages and download their PDF. Do not expose this development server to the internet.

After an assigned editor records acceptance for the latest version, the editor opens the submission and starts local production. A coordinator opens **Manage editor assignments**, selects the submission and then **Manage production assignments**, and assigns an eligible production editor. The production editor opens the task in their workspace, may download the accepted source PDF, enters fictional public author names and metadata, and uploads a separate fictional publication PDF. The submitting author signs in, downloads the current proof, and confirms the current metadata and PDF. The assigned production editor must explicitly confirm publication. Replacing the proof or changing metadata requires a new author confirmation. The system stores each test proof and checks its SHA-256 digest when confirming, publishing and downloading; the check is not virus scanning or a content review. Publication freezes the test record in this interface; correction, retraction and removal workflows are not implemented.

A production editor requires both an active journal role and an active assignment for that draft. An author, any invited reviewer, a person who has recused from editorial or production work, and the assigning coordinator cannot be assigned. A production editor may recuse; this also blocks their editorial and coordinator access to that manuscript. Recusal as editor likewise blocks their production access. Removing an assignment blocks subsequent requests to the production workspace, accepted source and private proofs; a download already in progress cannot be recalled. If a test article was already published, its public page and PDF remain available to anyone who can reach the test server. Assignment history, previous decisions and author confirmations remain. A person can have both editing and production roles only when each role is separately assigned. These checks do not identify every possible conflict, and role assignment must be reviewed by humans.

The confirmation belongs only to the submitting account and does not establish that all coauthors, editors or rights holders approved publication. The simulation does not provide proof annotations, volume and issue management, licence management, DOI registration, indexing or formal publication. It is not evidence of publication and must never be used for real manuscripts.

## Validation

From `backend/`:

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test publishing.tests
```

From the repository root, run `node --test tests/*.test.cjs` and `python3 scripts/sync-locales.py --check` for the unchanged static interface. The compiled Chinese message catalogue is committed; after changing `locale/zh_Hans/LC_MESSAGES/django.po`, regenerate it with `python manage.py compilemessages -l zh_Hans` (GNU gettext is required).

## Optional OpenAI metadata suggestions

The adapter is **disabled by default** and has been tested only with a simulated provider; no live OpenAI request was made during development. Local rule checks work without it. This is an API integration, not a claim of partnership or endorsement.

For fictional data in an isolated test environment only, an operator may enable it by setting `IPS_OPENAI_ENABLED=1`, setting `IPS_OPENAI_MODEL` to a model available to the API project, and setting `OPENAI_API_KEY` securely in the server environment. Never put the key in the repository, browser code, test data, logs or chat. An administrator must also enable **Allow OpenAI metadata suggestions** for the specific test journal. The author then grants consent for the exact title, abstract, version and task purpose; each author or authorised editor must confirm each request. A change to the title, abstract, version or task purpose requires fresh consent. Revocation blocks future requests but cannot recall a request already in progress or data already sent.

The request sends only the title and abstract, not PDF files, separate author account fields or reviewer records. Any personal information included in the title or abstract would still be sent. It uses the Responses API with `store: false`, but provider data-retention rules can still apply; this is not a zero-retention guarantee. API calls may cost money. Review the current [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data) and [Responses API documentation](https://developers.openai.com/api/docs/guides/migrate-to-responses) before enabling. Do not transmit real manuscripts or personal information from this prototype.

The model is asked for clarity suggestions rather than editorial decisions. Returned evidence must be an exact substring of the supplied text, but this does not prove that observations are correct or that embedded instructions were ignored. Results remain unverified, are never used to change editorial status, and must be reviewed by a person. Consent records, request metadata and bounded suggestions are saved in the local test database; results are visible only to the requester while they remain authorised for the submission. Revoking consent does not delete previous results. The test limit of three external requests per account per 24 hours is a local safeguard, not a production billing or distributed rate-limit control. A failed request is not retried automatically; an interrupted process may leave a request marked pending.

## Limits and before real use

This is an intentionally incomplete local test implementation, not a controlled pilot or production service. It has no self-service account registration, password recovery, multifactor authentication, login throttling, mail delivery, automated anonymisation or identity verification, reviewer deadlines, comprehensive conflict management, configurable editorial policies, formal publication, DOI, production-ready AI governance, backups, encryption at rest, virus scanning or production monitoring. The original PDF, separate reviewer PDF and response are versioned; submission title, abstract and additional files are not yet versioned. A reviewer can read the title and abstract before accepting. An author retains access to their own submission while the journal and account remain active, even if their author role is removed. A reviewer's journal role must remain active to access assigned work. Assignment and self-recusal do not replace a full conflict-of-interest policy or independent investigation.

The SQLite database and development server are unsuitable for concurrent production use. Before any real-data pilot, replace the development settings with a separately designed and security-reviewed deployment, durable transactional database, TLS, secret management, access and retention policies, upload limits at the reverse proxy, malware scanning, safe storage and download controls, audit review, backups and tested restoration. Review all threat and privacy requirements, including journal-specific editorial assignments and conflicts. Do not assume that passing the included tests proves security or publishing compliance.
