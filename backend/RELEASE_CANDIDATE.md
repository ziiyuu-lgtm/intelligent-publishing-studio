# Blocked release candidate

The local workflow can be tested with fictional records, but it is not a
production journal system. This candidate prepares part of the configuration
for later integration work. **It is not a production release or an instruction
to deploy one.** Do not enter real manuscripts or personal information.

The normal `config.settings` development profile, SQLite test database and local
publication simulation remain unchanged. The candidate is a separate,
explicitly selected `config.settings_release_candidate` profile. Its first
middleware blocks every HTTP request with a generic 503. The deployment check
also raises `publishing.E001`; no environment variable switches these blocks
off. The release guards must remain until the remaining requirements are
implemented and independently verified. Do not disable or work around them.

The candidate requires separately supplied, unique, randomly generated Django
and review-fingerprint keys, an exact host allowlist, a private data directory
outside the checkout, and credentials for a PostgreSQL database on loopback.
Missing or structurally invalid settings fail at startup, without printing
secrets. Store real credentials through an appropriate secret-management
mechanism; do not commit them or paste them into chat. Candidate dependencies
are listed separately in `requirements.release-candidate.txt`. The fictional
workflow, login-throttle, fictional invitation-mail, local registration and
simulated email-link tests ran against PostgreSQL on the VPS on 2026-11-04
(138 tests passed). The blocked release profile, production role
permissions and real-data workflows have not been validated by that test.

On POSIX systems the private data root must be owned by the account running
the application. If its `private-media` child already exists, it must be an
owner-only directory owned by that account, rather than a symlink or file.
Run candidate management commands as that same service account.
These startup checks do not inspect all nested paths or prevent later changes;
storage isolation and file-handling still require independent review.

The separate `config.settings_postgres_probe` profile and
`scripts/run_altavien_postgres_probe.sh` are for a single isolated, fictional
database compatibility test. The profile accepts only the Django test
command, a randomly named probe database and the local postgres OS account;
it must never serve web traffic. The script refuses non-loopback database
listeners, creates new names and preserves the probe databases for inspection.
The test uses a PostgreSQL administrator account to exercise compatibility;
it does not validate production database permissions or credentials.

The same probe runner has a separate `backup-drill` mode for a fictional
recovery rehearsal. It creates new random probe databases, adds an invented
submission and a fictional PDF, exports the isolated test database with
`pg_dump`, and restores the archive into another newly created random database.
It checks the restored submission, journal, author, version, file path and file
bytes; SHA-256 checks detect accidental changes to the sample archive or file.
Only the dedicated probe profile, local PostgreSQL socket, postgres OS account
and explicitly enabled fictional test are accepted. The new databases and a
private temporary directory are retained for inspection; the operating demo
database, files and services are not involved.
The four-test fictional backup drill passed on the VPS on 2026-11-04.

For separate reviewer PDFs, the local workflow now records a digest of the
specific file and version title, abstract and keywords checked by the editor.
The editor sees that version's metadata beside the check form. A changed or
missing file blocks invitations and reviewer downloads until an assigned editor
checks it again. Changed or legacy metadata is hidden on reviewer-facing pages
until rechecked. The checked file bytes are served directly rather than
reopening the file after verification. Legacy checks without both digests need
a fresh check. These digests detect changes but do not protect against someone
who can also modify the database, validate the editor's confirmation, or verify
PDF or metadata anonymity; independent review is still needed.
The isolated probe runner's `review-copy` mode runs the relevant tests against
two newly created, retained fictional PostgreSQL probe databases.
The reviewer-PDF and metadata checks passed eight tests against the VPS probe's
fictional PostgreSQL database on 2026-11-04.

For a closed review round, an assigned editor may separately recheck the
historical reviewer PDF and version metadata. The confirmation is tied to the
editor, round, file digest and metadata. It can restore visibility of recorded
historical metadata but does not reopen the round or enable reviewer downloads.
No check is offered while an open round uses the same version. This remains a
manual, fictional-data workflow and does not establish anonymity.

In the local publication simulation, private proof and published-PDF downloads
return the exact bounded bytes whose PDF header and stored SHA-256 were checked.
This closes a gap where a file could change between the check and a second
open. It does not parse PDFs, scan them for malware, or protect against someone
who can also change the database digest.

This drill is not a backup of existing data or a production backup system. It
uses one quiescent fictional file and no concurrent writers; a real service
needs a coordinated database-and-file snapshot, authenticated integrity,
encryption, off-host copies, retention, monitoring and regularly tested
recovery. Temporary files may be removed by the operating system. Do not use
the drill with real manuscripts or personal data.

An optional local-only login-throttle experiment is disabled by default. It
requires a separate private key, works only in DEBUG, and counts attempts by
direct source IP and source/username pair with pseudonymous stored keys. Its
fixed window and direct-IP assumption are not a complete production abuse
defence; proxy trust, distributed attacks, account recovery, retention and
operational monitoring must be designed and independently tested first.

An optional review-invitation mail queue is limited to fictional `.invalid`
addresses, is disabled by default and works only in DEBUG. Invitation creation
and queue insertion share a transaction. A manual command processes pending
items using Django's in-memory email backend directly; it never uses SMTP or
delivers an actual email. The recipient is rechecked, and changed addresses,
expired invitations or revoked access are suppressed. This local simulation
is not a production email system: real delivery requires verified addresses,
consent and privacy rules, crash and retry handling, provider configuration,
retention, audit and independent security review.

An optional fictional account-registration demonstration is disabled by
default and works only in DEBUG with `IPS_LOCAL_REGISTRATION_ENABLED=1` and a
separate `IPS_LOCAL_REGISTRATION_KEY` of at least 32 characters. It accepts only
test-prefixed usernames and invented `.invalid` addresses. The confirmation
code is deliberately displayed in the same browser, so it does not verify a
mailbox or a person's identity; no email is sent. Accounts start inactive,
confirmation expires, and no journal or organisation role is granted.
This demonstration is not a real account lifecycle or a substitute for
production registration, recovery, abuse prevention or verified email.

An additional experiment, `IPS_LOCAL_MAIL_VERIFICATION_ENABLED=1`, requires
that fictional registration also be enabled. It creates a fictional message
for an `.invalid` address in the current process's in-memory outbox, using the
in-memory email backend directly even if SMTP is configured elsewhere. A test
can retrieve its link and simulate opening it in another browser; the link
token is in the URL fragment and confirmation requires a separate CSRF-checked
POST. The fragment is still visible to the browser and its scripts or
extensions. It never sends or verifies a real email. This remains local-only,
default-off and disabled in the blocked release profile. The experiment has no
durable delivery queue, resend or recovery workflow, provider integration,
or production abuse/identity protections; successful synthetic tests do not
establish those properties. The in-memory message is scheduled only after the
database transaction commits, but it is not durable; a process interruption or
callback failure can lose it. These checks do not exercise actual mail delivery.

The test confirmation fingerprint now also depends on the password hash. A
password change invalidates a pending test link. Pending links issued by the
earlier test code are also invalid after upgrading; already active accounts
are not changed.

The optional `check_smtp_config` management command checks the format of a
proposed SMTP host, port, TLS mode, authentication details, Altavien sender
address and timeout. It does not connect, verify credentials or domain
ownership, check DNS, send a message, or enable the SMTP backend. Supply any
eventual credentials through an appropriate secret-management mechanism and
never print or paste them into chat. Real delivery still needs a reviewed
provider integration, durable processing and end-to-end testing.

The profile sets HTTPS-only cookies and redirection, but does not install TLS,
configure a reverse proxy or trust proxy-provided headers. HSTS subdomain and
preload options remain disabled pending a scope and certificate review; Django
may flag these as `security.W005` and `security.W021`. Email and AI are disabled,
and attempted real email delivery raises an error. Published test articles
and other DEBUG-only features remain unavailable. No real notification
delivery, DOI, backup, restore or real-user account workflow has been added here.

## Release work still required

- Implement and test production workflows independently from the fictional,
  DEBUG-only publication path, with role and tenant isolation.
- Add verified account lifecycle, login abuse protections and a reviewed,
  consent-aware mail-delivery system; test delivery without exposing data.
- Test the complete workflow and database migrations with real PostgreSQL,
  including concurrent requests and failed transactions.
- Design secure TLS, proxy, process management and upload/storage controls.
- Implement and exercise coordinated database and file backups and recovery,
  with retention, monitoring and an independent recovery copy.
- Complete privacy, security, accessibility and operational reviews and test
  upgrades and incident handling before considering any real-data pilot.

Changing `DEBUG` or removing the test-data wording alone does not implement
these safeguards. The candidate profile must stay blocked until this work is
complete and a separate release decision is made.
