#!/bin/sh
set -eu

. /scripts/secret-env.sh
load_pgpassword

: "${1:?SQL file is required}"
exec psql --no-psqlrc --set=ON_ERROR_STOP=1 --file="$1"
