#!/bin/sh
# Nightly Popping backup: a compressed Postgres dump plus a copy of .env,
# kept for 14 days in /root/backups (root-only; the dump and .env both
# hold API keys and passwords).
#
# Install (as root):
#   cp ops/popping-backup.sh /usr/local/bin/popping-backup && chmod 700 /usr/local/bin/popping-backup
#   echo '30 10 * * * root /usr/local/bin/popping-backup >> /var/log/popping-backup.log 2>&1' > /etc/cron.d/popping-backup
#
# Restore a dump into the running stack:
#   cd /opt/popping && docker compose exec -T postgres sh -c \
#     'pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /root/backups/popping-XXXX.dump
set -eu

DIR=/root/backups
KEEP_DAYS=14
STAMP=$(date -u +%Y%m%d-%H%M)

umask 077
mkdir -p "$DIR"
cd /opt/popping

# Dump to a temp name first so a failed dump never looks like a good one.
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$DIR/.popping-$STAMP.dump.tmp"
mv "$DIR/.popping-$STAMP.dump.tmp" "$DIR/popping-$STAMP.dump"
cp .env "$DIR/env-$STAMP"

find "$DIR" -maxdepth 1 -type f \( -name 'popping-*.dump' -o -name 'env-*' \) -mtime +"$KEEP_DAYS" -delete
find "$DIR" -maxdepth 1 -type f -name '.popping-*.tmp' -mtime +1 -delete

echo "$(date -u +%FT%TZ) backup ok: popping-$STAMP.dump ($(du -h "$DIR/popping-$STAMP.dump" | cut -f1))"
