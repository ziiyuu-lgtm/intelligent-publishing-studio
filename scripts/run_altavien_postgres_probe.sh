#!/usr/bin/env bash
# Fictional-data compatibility test only. Run by root from a dedicated checkout.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo STOP_REQUIRES_ROOT
    exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_dir=$(cd -- "$script_dir/.." && pwd -P)
if [ "$source_dir" != /srv/altavien-pg-probe/source ]; then
    echo STOP_UNEXPECTED_CHECKOUT
    exit 1
fi
if [ "$(stat -c %U "$source_dir")" != postgres ]; then
    echo STOP_UNEXPECTED_OWNER
    exit 1
fi
for command in pg_isready psql createdb runuser ss python3; do
    if ! command -v "$command" >/dev/null; then
        echo "STOP_MISSING_COMMAND: $command"
        exit 1
    fi
done
if ! pg_isready -q -h 127.0.0.1 -p 5432; then
    echo STOP_POSTGRES_NOT_READY
    exit 1
fi
listeners=$(ss -H -lnt "sport = :5432")
if [ -z "$listeners" ]; then
    echo STOP_NO_POSTGRES_LISTENER
    exit 1
fi
while read -r state recvq sendq address peer extra; do
    case "$address" in
        127.0.0.1:5432|'[::1]:5432'|::1:5432) ;;
        *) echo "STOP_NONLOCAL_LISTENER: $address"; exit 1 ;;
    esac
done <<< "$listeners"

probe_suffix=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
probe_db="altavien_probe_${probe_suffix}"

# Both names are random and created explicitly. A collision or failure stops
# the script before Django runs. No existing database is dropped or reset.
runuser -u postgres -- createdb -h /var/run/postgresql -p 5432 -U postgres --template=template0 "$probe_db"
echo "CREATED_BASE_DATABASE: $probe_db"
runuser -u postgres -- createdb -h /var/run/postgresql -p 5432 -U postgres --template=template0 "${probe_db}_test"
echo "CREATED_TEST_DATABASE: ${probe_db}_test"

cd "$source_dir/backend"
runuser -u postgres -- env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/var/lib/postgresql \
    DJANGO_SETTINGS_MODULE=config.settings_postgres_probe \
    ALTAVIEN_PROBE_CONFIRM_SYNTHETIC=1 \
    ALTAVIEN_PROBE_DB="$probe_db" \
    "$source_dir/.venv/bin/python" manage.py test \
    publishing.tests.test_workflow \
    publishing.tests.test_production \
    publishing.tests.test_submission_checklist \
    publishing.tests.test_editor_assignments \
    publishing.tests.test_local_login_throttle \
    publishing.tests.test_local_review_mail \
    publishing.tests.test_local_registration \
    publishing.tests.test_local_mail_verification \
    --keepdb --noinput -v 1

echo ALTAVIEN_POSTGRES_PROBE_PASSED
echo PROBE_DATABASES_RETAINED_FOR_REVIEW
