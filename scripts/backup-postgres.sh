#!/bin/sh
# Dumps the Compose `db` service to <directory>/football-<UTC timestamp>.dump (default ./backups).
# A dump that fails or does not pass `pg_restore --list` is deleted and the script exits non-zero,
# so a failed run never leaves an empty file and never prunes older dumps.
# Dumps older than BACKUP_RETENTION_DAYS (default 7) are pruned only after a verified dump.
set -eu

backup_directory=${1:-./backups}
retention_days=${BACKUP_RETENTION_DAYS:-7}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_directory/football-$timestamp.dump"
partial="$target.partial"

umask 077
mkdir -p "$backup_directory"
trap 'rm -f "$partial"' EXIT

docker compose exec -T db sh -c \
    'pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom' > "$partial"

if [ ! -s "$partial" ]; then
    echo "backup failed: pg_dump wrote nothing" >&2
    exit 1
fi
if ! docker compose exec -T db pg_restore --list < "$partial" > /dev/null; then
    echo "backup failed: pg_restore cannot read the dump" >&2
    exit 1
fi

mv "$partial" "$target"
find "$backup_directory" -type f -name 'football-*.dump' -mtime "+$retention_days" -delete
echo "$target"
