# Updating the isolated ALTAVIEN issue workflow

This installer updates only the existing fictional test service at
`/srv/altavien-test` to application commit
`35c25bd53b7a4cfa97d531e377151c0a9791749d`.
It does not change a production profile or operate on an external journal site.

It recognizes the exact runtime files of the online-first, ordered-issue and
append-fixed versions. Unknown edits, symlinks, changed dependencies, a different
service owner or a listener outside loopback stop the upgrade before the site is
changed. Download and verify the installer from a reviewed, immutable Git commit.
The source repository at /srv/altavien-pg-probe/source must contain the pinned
application commit; its working checkout is not advanced by the installer.

Run on the existing test VPS as root:

```sh
python3 /path/to/verified/install_altavien_issue_release.py --check
python3 /path/to/verified/install_altavien_issue_release.py
```

The check mode leaves application files, records and the running service alone;
it uses a root-owned lock to exclude concurrent upgrades. The upgrade prepares
a private code/service/database backup and first rehearses the change on a
database copy. It stops the test service only after that succeeds, then takes a
fresh SQLite snapshot and records media checksums. It applies only the pinned
runtime files and, when needed, migration 0054's nullable article-order column.
Every pre-existing database row and saved article order is compared before and
after the migration; media bytes are checked before the service restarts.

A successful run prints ALTAVIEN_ISSUE_RELEASE_UPGRADE_READY and the private
backup directory. Re-running the same installer verifies the current version
without stopping the service or creating another upgrade backup.

If installation or the restarted site's checks fail, the installer attempts
to restore the original code and service. It never restores an earlier database
or media snapshot over new writes. The additive nullable column may remain so
that saved order values and writes after restart survive recovery. A recovery
failure prints ALTAVIEN_RECOVERY_NEEDS_ATTENTION and needs investigation;
that message is not a successful recovery.

The GitHub Actions rehearsal uses freshly created fictional sites on ephemeral
runners. It covers all three baselines, unknown edits, symlinks, concurrent runs,
failure after migration, failure after a restarted service receives new records
and files, compatibility of the previous code with the retained nullable column,
successful completion and repeated execution. It does not access the VPS.
