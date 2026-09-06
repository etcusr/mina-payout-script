#!/usr/bin/env bash
# SSH tunnel to a remote Mina node: daemon GraphQL + archive Postgres.
#
# Everything in this project talks to 127.0.0.1, so with the tunnel up the
# scripts do not care whether the node is local or on the other side of the
# planet.
#
#   ./scripts/tunnel.sh my-node            # both ports, stays in foreground
#   ./scripts/tunnel.sh my-node --bg       # background, writes a pid file
#   ./scripts/tunnel.sh --stop             # kill a backgrounded tunnel
#
# The host argument is anything ssh understands: an alias from ~/.ssh/config,
# user@host, etc. Defaults to $MINA_SSH_HOST when set.
#
# Ports (override with env vars):
#   GRAPHQL_PORT   local 3085 -> remote 3085   daemon GraphQL
#   PG_PORT        local 5432 -> remote 5432   archive Postgres
#
# If your daemon publishes GraphQL on a different remote port (a Docker
# mapping, say), set REMOTE_GRAPHQL_PORT.

set -euo pipefail

GRAPHQL_PORT="${GRAPHQL_PORT:-3085}"
REMOTE_GRAPHQL_PORT="${REMOTE_GRAPHQL_PORT:-$GRAPHQL_PORT}"
PG_PORT="${PG_PORT:-5432}"
REMOTE_PG_PORT="${REMOTE_PG_PORT:-$PG_PORT}"
PIDFILE="${TMPDIR:-/tmp}/mina-payout-tunnel.pid"

if [ "${1:-}" = "--stop" ]; then
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
    echo "tunnel stopped"
  else
    echo "no running tunnel found"
    rm -f "$PIDFILE"
  fi
  exit 0
fi

HOST="${1:-${MINA_SSH_HOST:-}}"
if [ -z "$HOST" ]; then
  echo "Usage: $0 <ssh-host> [--bg]"
  echo "   or: export MINA_SSH_HOST=my-node"
  exit 1
fi
shift || true

# Warn instead of failing: a tunnel may already be up from another shell.
for p in "$GRAPHQL_PORT" "$PG_PORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "note: port $p is already listening - existing tunnel? skipping it"
  fi
done

SSH_ARGS=(-N
  -L "${GRAPHQL_PORT}:127.0.0.1:${REMOTE_GRAPHQL_PORT}"
  -L "${PG_PORT}:127.0.0.1:${REMOTE_PG_PORT}"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  "$HOST")

if [ "${1:-}" = "--bg" ]; then
  ssh -f "${SSH_ARGS[@]}"
  # -f backgrounds inside ssh, so find the pid by the forward we just asked for
  pgrep -f "ssh.*-L ${GRAPHQL_PORT}:127.0.0.1:${REMOTE_GRAPHQL_PORT}.*${HOST}" \
    | head -1 > "$PIDFILE"
  echo "tunnel up in background (pid $(cat "$PIDFILE"))"
  echo "  GraphQL  127.0.0.1:${GRAPHQL_PORT}"
  echo "  Postgres 127.0.0.1:${PG_PORT}"
  echo "stop with: $0 --stop"
else
  echo "tunnel to ${HOST}:  GraphQL ${GRAPHQL_PORT}, Postgres ${PG_PORT}"
  echo "Ctrl+C to stop"
  exec ssh "${SSH_ARGS[@]}"
fi
