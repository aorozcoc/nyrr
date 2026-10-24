#!/usr/bin/env bash
# Lite-mode persistence: the monitor's PostgreSQL state lives in a git branch.
#
#   state_store.sh restore   # load the latest saved state into the job's PostgreSQL (or create the schema)
#   state_store.sh save      # dump the schema and commit it to the state branch
#
# PostgreSQL runs as a throw-away service container inside each GitHub Actions job. psql/pg_dump are run
# inside that container (PG_CONTAINER) so client and server versions always match. Without PG_CONTAINER
# (local testing) the local psql/pg_dump are used with PGURL.
#
# The saved dump contains public NYRR shift data and run history only: recipients are stored as
# pseudonymous keys ("recipient-1"), never addresses.
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-monitor-state}"
STATE_PATH="state/nyrr_monitor.sql.gz"
MIGRATION="db/migrations/001_initial_schema.sql"

psql_cmd() {
  if [ -n "${PG_CONTAINER:-}" ]; then
    docker exec -i "$PG_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -q "$@"
  else
    psql "$PGURL" -v ON_ERROR_STOP=1 -q "$@"
  fi
}

pg_dump_cmd() {
  if [ -n "${PG_CONTAINER:-}" ]; then
    docker exec "$PG_CONTAINER" pg_dump -U postgres -d postgres --schema=nyrr_monitor --no-owner --no-privileges
  else
    pg_dump "$PGURL" --schema=nyrr_monitor --no-owner --no-privileges
  fi
}

fetch_state() {
  git fetch --quiet --depth=1 origin "refs/heads/${STATE_BRANCH}:refs/remotes/origin/${STATE_BRANCH}" 2>/dev/null
}

case "${1:-}" in
  restore)
    if fetch_state && git cat-file -e "origin/${STATE_BRANCH}:${STATE_PATH}" 2>/dev/null; then
      git show "origin/${STATE_BRANCH}:${STATE_PATH}" | gunzip | psql_cmd
      echo "state restored from ${STATE_BRANCH} ($(git log -1 --format=%cI "origin/${STATE_BRANCH}"))"
    else
      psql_cmd < "$MIGRATION"
      echo "no saved state yet: created a fresh schema"
    fi
    ;;
  save)
    tmp="$(mktemp)"
    pg_dump_cmd | gzip -n > "$tmp"
    test -s "$tmp"
    export GIT_AUTHOR_NAME="nyrr-monitor-bot" GIT_AUTHOR_EMAIL="actions@users.noreply.github.com"
    export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
    blob="$(git hash-object -w "$tmp")"
    sub="$(printf '100644 blob %s\tnyrr_monitor.sql.gz\n' "$blob" | git mktree)"
    tree="$(printf '040000 tree %s\tstate\n' "$sub" | git mktree)"
    msg="state: ${GITHUB_RUN_ID:-local} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for attempt in 1 2 3; do
      parent=""
      if fetch_state; then parent="$(git rev-parse "origin/${STATE_BRANCH}")"; fi
      commit="$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "$msg")"
      if git push --quiet origin "${commit}:refs/heads/${STATE_BRANCH}"; then
        echo "state saved to ${STATE_BRANCH} (${commit:0:8}, $(stat -c %s "$tmp") bytes)"
        exit 0
      fi
      echo "push rejected (attempt ${attempt}); retrying" >&2
      sleep $((attempt * 3))
    done
    echo "could not save state" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 restore|save" >&2
    exit 2
    ;;
esac
