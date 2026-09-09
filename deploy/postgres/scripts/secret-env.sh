#!/bin/sh
set -eu

load_pgpassword() {
  : "${PGPASSWORD_FILE:?PGPASSWORD_FILE is required}"
  if [ ! -f "$PGPASSWORD_FILE" ] || [ -L "$PGPASSWORD_FILE" ]; then
    echo "PostgreSQL password secret must be a regular, non-symlink file" >&2
    exit 1
  fi
  PGPASSWORD=$(tr -d '\r\n' <"$PGPASSWORD_FILE")
  if [ -z "$PGPASSWORD" ]; then
    echo "PostgreSQL password secret is empty" >&2
    exit 1
  fi
  export PGPASSWORD
}
