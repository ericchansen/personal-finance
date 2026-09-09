#!/bin/sh
set -eu

. /scripts/secret-env.sh
load_pgpassword

source_migrations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.schema_migrations"
)
source_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.transaction_observations"
)
source_conformance_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.conformance_observations"
)
source_conformance_commits=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.conformance_batch_commits"
)
source_artifact_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.artifact_observations"
)

dump_file=/tmp/finance-test.dump
trap 'rm -f "$dump_file"; dropdb --if-exists finance_restore' EXIT
pg_dump --format=custom --file="$dump_file"
dropdb --if-exists finance_restore
createdb finance_restore
pg_restore --dbname=finance_restore --exit-on-error "$dump_file"

restored_migrations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --dbname=finance_restore \
    --command="SELECT count(*) FROM finance.schema_migrations"
)
restored_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --dbname=finance_restore \
    --command="SELECT count(*) FROM finance.transaction_observations"
)
restored_conformance_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --dbname=finance_restore \
    --command="SELECT count(*) FROM finance.conformance_observations"
)
restored_conformance_commits=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --dbname=finance_restore \
    --command="SELECT count(*) FROM finance.conformance_batch_commits"
)
restored_artifact_observations=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --dbname=finance_restore \
    --command="SELECT count(*) FROM finance.artifact_observations"
)

test "$source_migrations" = "$restored_migrations"
test "$source_observations" = "$restored_observations"
test "$source_conformance_observations" = "$restored_conformance_observations"
test "$source_conformance_commits" = "$restored_conformance_commits"
test "$source_artifact_observations" = "$restored_artifact_observations"
echo "PostgreSQL dump/restore round trip passed."
