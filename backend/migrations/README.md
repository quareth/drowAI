# Database Migrations

This directory contains Alembic database migrations for DrowAI.

## Configuration

Product startup paths do not require a user-authored `.env`. Docker and local
Python launchers create generated config/secrets first, then run Alembic with
the generated `DATABASE_URL`. A process `DATABASE_URL` remains supported as a
developer override.

## Running Migrations

### From Host (Local Development)

```bash
# From backend directory
cd backend
alembic upgrade head

# Or from project root
alembic -c backend/alembic.ini upgrade head
```

### In Docker

```bash
# One-liner
docker exec -it drowai-backend sh -c "cd /app/backend && alembic upgrade head"

# Or interactive
docker exec -it drowai-backend sh
cd /app/backend
alembic upgrade head
```

## Common Commands

```bash
# Apply all pending migrations
alembic upgrade head

# Rollback last migration
alembic downgrade -1

# Rollback all migrations
alembic downgrade base

# Show current revision
alembic current

# Show migration history
alembic history

# Generate new migration (auto-detect changes)
alembic revision --autogenerate -m "description of change"

# Generate empty migration (manual)
alembic revision -m "description of change"
```

## Migration Files

The active history starts at `versions/0001_initial_current_schema.py`. That
baseline must create a complete fresh schema without `Base.metadata.create_all()`
or `alembic stamp`. Future schema changes append normal Alembic revisions after
that baseline.

## Creating New Migrations

1. Make changes to models in `backend/models/<domain>.py` (per-domain modules under the `backend/models/` package)
2. Generate migration:
   ```bash
   cd backend
   alembic revision --autogenerate -m "describe your change"
   ```
3. Review generated migration in `versions/`
4. Test migration:
   ```bash
   alembic upgrade head
   alembic downgrade -1
   alembic upgrade head
   ```

## Migration Test Pattern

Schema architecture tests live under `backend/tests/migrations/`. They should
verify the Alembic graph, fresh upgrade behavior, and required baseline objects.
New migration tests should exercise `alembic upgrade head` from an empty DB or a
known previous revision instead of calling ORM `create_all()`.

## Troubleshooting

### "No 'script_location' key found"
Run from the `backend/` directory or specify config path:
```bash
alembic -c backend/alembic.ini upgrade head
```

### "DATABASE_URL not set"
Run through the Docker/local launcher so generated config is loaded, or export a
developer override:
```bash
export DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
```

### "Target database is not up to date"
The database has pending migrations. Run:
```bash
alembic upgrade head
```
