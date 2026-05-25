#!/bin/bash
# Run psql inside the compose db container. Use VB_TEST_DB env to target a specific db.
# Usage: ./scripts/psql.sh openbrain -c "SELECT 1"
#        ./scripts/psql.sh openbrain_test -f db/schema.sql
set -euo pipefail
DB="${1:-openbrain}"
shift || true
exec env DOCKER_CONFIG=/tmp/docker-noauth docker compose exec -T db psql -U postgres -d "$DB" "$@"
