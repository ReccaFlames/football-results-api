#!/bin/sh
set -eu

backup_directory=${1:-./backups}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup_directory"

docker compose exec -T db sh -c 'pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom' > "$backup_directory/football-$timestamp.dump"

find "$backup_directory" -type f -name 'football-*.dump' -mtime +7 -delete
echo "$backup_directory/football-$timestamp.dump"

