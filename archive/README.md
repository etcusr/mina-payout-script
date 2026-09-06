# Mina Archive Node

**English** · [Русский](README.ru.md)

A self-hosted archive node, kept completely separate from your block producer.
The payout scripts read their block data from this node's Postgres instead of
from public explorer APIs, which have a habit of disappearing.

## Architecture

The stack runs on its own docker network, `mina-net`. A block producer normally
sits on the default `bridge` network and is never touched, so its uptime is not
affected.

```
[bridge - untouched]
  └─ mina                 your block producer

[mina-net - this stack]
  ├─ postgres             archive database, exposed on 127.0.0.1:5432
  ├─ bootstrap_db         one-shot: downloads and restores a mainnet dump
  ├─ mina_archive         listens on 3086, writes incoming blocks to Postgres
  ├─ mina_node            non-producing daemon, syncs the chain, feeds the archive
  └─ missing_blocks_guardian
                          backfills gaps from precomputed-block CDNs
```

## Files

```
archive/
├── docker-compose.yml       the whole stack
├── scripts/
│   └── blocks-guardian.sh   gap filler with CDN fallback (see below)
├── backfill-epoch.sh        bulk-import a historical height range
├── mina-archive-refresh.sh  restore a newer dump when one is published
├── init-scripts/            legacy, no longer needed
├── pgdata/                  Postgres data (created automatically)
├── follower-config/         the follower's .mina-config (created automatically)
└── daemon-restart.sh        DEPRECATED, kept for reference only
```

## Install

### 1. Copy the stack to the server

```bash
rsync -av --exclude pgdata --exclude follower-config --exclude cache \
  ./archive/ your-node:~/mina-archive/
```

### 2. Open the libp2p port

The follower daemon needs inbound libp2p connections. It uses **8303** so it
cannot clash with a producer already using 8302.

```bash
sudo ufw allow 8303/tcp
```

### 3. Start it

```bash
cd ~/mina-archive
docker compose up -d
docker compose ps
```

`bootstrap_db` downloads a mainnet dump (several GB) and restores it. That takes
a while on first run; the other containers wait for it to finish.

### 4. Check

```bash
# Tables are in place
docker exec mina-archive-pg psql -U postgres -d archive -c '\dt' | head -30

# The archive process is listening
docker logs mina-archive 2>&1 | tail -20

# The follower is syncing
docker logs --tail 30 -f mina-follower

# Blocks are arriving
watch -n 10 "docker exec mina-archive-pg psql -U postgres -d archive -c \
  \"SELECT COUNT(*) AS blocks, MAX(height) AS tip FROM blocks;\""
```

The follower takes a few hours to catch up to the current tip. From then on
every new block lands in Postgres automatically.

## The block guardian

The Mina Foundation ships a `missing-blocks-guardian` script that fills gaps in
the archive from a bucket of precomputed blocks. It hardcodes a single S3
bucket, and when the Foundation deleted that bucket the script began retrying
the same missing block forever — hundreds of log lines per second, no progress,
indefinitely.

`scripts/blocks-guardian.sh` replaces it:

- tries several mirrors in order (GCS first, then S3)
- validates the JSON before handing it to `mina-archive-blocks`
- blacklists blocks that are unavailable everywhere instead of looping on them
- asks the daemon which blocks are actually missing rather than guessing

To add another mirror, append to the `SOURCES` array in that script.

## Connecting the payout scripts

The scripts talk to `127.0.0.1`, so tunnel both ports from your workstation:

```bash
./scripts/tunnel.sh your-node --bg
```

Then in `config.yml`:

```yaml
ARCHIVE_DB_URL: "postgresql://postgres:<password>@127.0.0.1:5432/archive"
```

Use whatever `POSTGRES_PASSWORD` you set in `docker-compose.yml`. The default in
the committed file is `postgres`, which is fine while the port is bound to
`127.0.0.1` only — change it if you expose the database at all.

## Maintenance

```bash
# Back up
docker exec mina-archive-pg pg_dump -U postgres archive | gzip > archive-$(date +%F).sql.gz

# Database size
docker exec mina-archive-pg psql -U postgres -d archive -c \
  "SELECT pg_size_pretty(pg_database_size('archive'));"

# Stop the archive stack (the producer is not affected)
cd ~/mina-archive && docker compose down

# Wipe everything, including data
cd ~/mina-archive && docker compose down -v && rm -rf pgdata follower-config
```

### Moving to a newer dump

The Foundation publishes a mainnet dump roughly monthly. To rebuild from a newer
one, update `DUMP_DATE` in `docker-compose.yml` and recreate the database, or
use `mina-archive-refresh.sh`.

### Importing a historical range

`backfill-epoch.sh` pulls a height range straight from the precomputed-block
bucket and imports it, which is useful when you need epochs older than your
dump. It expects the database password in the environment:

```bash
PGPASSWORD=... ./backfill-epoch.sh <min-height> <max-height>
```

## Rollback

```bash
cd ~/mina-archive
docker compose down
docker ps | grep mina    # only the producer should remain
```

The producer container stays on the default bridge network throughout and is
never reconfigured by this stack.
