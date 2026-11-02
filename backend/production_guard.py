"""
Fails startup loudly, on purpose, if ENVIRONMENT=production is declared
but one of the dev-only defaults (SECRET_KEY, FIELD_ENCRYPTION_KEY, or
the docker-compose Postgres password baked into DATABASE_URL) is still
active. A misconfigured deployment that silently runs with a known,
public secret is worse than one that refuses to start — this makes that
specific mistake impossible to make quietly.

Only runs the check when ENVIRONMENT=production is explicitly set; local
dev (the default) is unaffected, same as every other env var in this
codebase that has a dev-safe fallback.
"""

import os

_DEV_SECRET_KEY = "dev-secret-change-before-production"
_DEV_FIELD_ENCRYPTION_KEY = "emfWTE1sYWkfI1HoyeBueyaEdLGeHroETxyRE7cpipo="
_DEV_DB_PASSWORD_MARKER = "slatepass"


def check_production_safety() -> None:
    if os.getenv("ENVIRONMENT", "development").lower() != "production":
        return

    problems = []
    if os.getenv("SECRET_KEY", _DEV_SECRET_KEY) == _DEV_SECRET_KEY:
        problems.append("SECRET_KEY is still the dev default")
    if os.getenv("FIELD_ENCRYPTION_KEY", _DEV_FIELD_ENCRYPTION_KEY) == _DEV_FIELD_ENCRYPTION_KEY:
        problems.append("FIELD_ENCRYPTION_KEY is still the dev default (or unset)")
    if _DEV_DB_PASSWORD_MARKER in os.getenv("DATABASE_URL", ""):
        problems.append("DATABASE_URL still contains the docker-compose dev password")

    if problems:
        raise RuntimeError(
            "Refusing to start with ENVIRONMENT=production while still using dev defaults: "
            + "; ".join(problems)
            + ". Set real values for these before deploying anywhere reachable."
        )
