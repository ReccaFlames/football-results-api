---
name: database-migration
description: Change the PostgreSQL schema of the Football Results API with Alembic. Use when adding/removing columns or tables, changing indexes or constraints, or when ORM models in app/db_models.py change.
---

# Database migration

The app never calls `create_all`; Alembic in `migrations/` owns the schema. The API container runs
`alembic upgrade head` on start (`docker-entrypoint.sh`).

## Steps

1. Edit ORM models in `app/db_models.py`.
2. Mirror field changes in Pydantic models (`app/models.py`) and the mapping code in
   `app/repository.py` (`replace_competition`, `get_results`, seeding) — columns are copied
   explicitly, field by field.
3. Generate against a running database at `head`:

   ~~~bash
   docker compose up -d db
   uv run alembic upgrade head
   uv run alembic revision --autogenerate -m "short description"
   ~~~

4. Rename the file to `migrations/versions/000N_snake_case.py` and set
   `revision = "000N_snake_case"` and `down_revision` to the previous revision id (see existing
   files; `0001` is the only id without a suffix). Inspect it by hand:
   - new non-null columns need a `server_default` or a data backfill;
   - autogenerate misses renames (it emits drop + add — rewrite as `alter_column`);
   - provide a working `downgrade()`.
5. Validate:

   ~~~bash
   uv run alembic upgrade head --sql > /dev/null   # offline render
   uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
   uv run pytest -q && uv run ruff check .
   ~~~

6. Update the database-table list in `README.md` if tables changed. Remind the user to back up
   production (`scripts/backup-postgres.sh`) before deploying.
