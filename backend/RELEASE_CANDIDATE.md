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
are listed separately in `requirements.release-candidate.txt`. PostgreSQL and
the driver have not yet been integration-tested as part of this change.

The separate `config.settings_postgres_probe` profile and
`scripts/run_altavien_postgres_probe.sh` are for a single isolated, fictional
database compatibility test. The profile accepts only the Django test
command, a randomly named probe database and the local postgres OS account;
it must never serve web traffic. The script refuses non-loopback database
listeners, creates new names and preserves the probe databases for inspection.
The test uses a PostgreSQL administrator account to exercise compatibility;
it does not validate production database permissions or credentials.

The profile sets HTTPS-only cookies and redirection, but does not install TLS,
configure a reverse proxy or trust proxy-provided headers. HSTS subdomain and
preload options remain disabled pending a scope and certificate review; Django
may flag these as `security.W005` and `security.W021`. Email and AI are disabled,
and attempted email delivery raises an error. Published test articles and
other DEBUG-only features remain unavailable. No notification delivery, DOI,
backup, restore or real-user account workflow has been added here.

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
