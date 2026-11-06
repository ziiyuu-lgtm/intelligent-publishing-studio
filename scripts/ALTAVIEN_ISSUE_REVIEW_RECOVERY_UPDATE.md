# Updating the isolated issue-review recovery

This update is limited to the fictional ALTAVIEN test service. It installs the
issue-review recovery from application commit
`3ffd1e2a58a8540f333d35dcfa699da467a329ac` on the exact previous
issue-review baseline. Unknown application edits, a different service, an
unexpected database state or untrusted installer bytes stop the update.

On Windows, run the verified repository launcher from PowerShell:

```powershell
.\scripts\resume_altavien_issue_review_recovery.ps1 -SshTarget root@your-test-host
```

The launcher requires an already trusted SSH host key and never stores a
password. It fetches the development branch, checks the application target is
in its history, and verifies the new installer by SHA-256. The new installer
also verifies the exact pinned bytes of the previous guarded installer before
using its existing preflight, staging, backup, lock, preservation and rollback
mechanisms. The working Git checkout on the server is not changed.

The update checks the site, makes a private backup, rehearses on a database
copy, briefly stops the test service, applies the reviewed files and restarts
the service. It checks existing records, saved article order and media bytes.
It neither releases an issue nor sends email. A repeated run checks the current
version without another restart. Database and media snapshots are never
restored over new writes during recovery.

The update runs as a detached systemd service. Keep `UPDATE_UNIT` from the
output; if the connection ends before completion, inspect that unit's journal
before retrying. `ALTAVIEN_REMOTE_UPDATE_VERIFIED` means the final preflight
passed. `UPDATE_STILL_RUNNING_UNIT` means the job is still running, not that
it has failed. `ALTAVIEN_RECOVERY_NEEDS_ATTENTION` requires investigation.

The installer and launcher rehearsals use only disposable CI fixtures and do
not connect to the VPS or test real journal records.
