#!/bin/sh
# Container entrypoint: migrate the database, optionally seed demo data, then start the bot.
set -e

# PID 1 ignores SIGTERM unless it is trapped: stop cleanly if the platform stops us during migrations.
trap 'echo "Stopped during startup"; exit 143' TERM INT

echo "Applying database migrations..."
python -m alembic upgrade head

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
  echo "Seeding DEMO catalog if the catalog is empty (SEED_DEMO_DATA=true)..."
  python -m scripts.seed_demo --if-empty || echo "Demo seeding failed, starting the bot anyway"
fi

# exec: the bot becomes PID 1 and receives SIGTERM for a graceful shutdown
exec python -m app
