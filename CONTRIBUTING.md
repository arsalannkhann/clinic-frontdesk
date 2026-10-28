# Contributing

Use branches named `codex/<topic>` or your team's agreed prefix. Keep one behavioral slice per PR. Install the locked development requirements from README. Use synthetic fixtures only. Explain the trigger, resulting behavior, tests, and remaining limitations in the PR.

Run Ruff and both pure and PostgreSQL integration tests before review. API changes also require `clinic-voice export-openapi > docs/openapi.json`. Include a forward SQL migration for schema changes, rollback/recovery notes for destructive changes, and concurrency tests when booking semantics change.

Dependency updates: use `uv pip compile pyproject.toml --universal -o requirements.lock` and `uv pip compile pyproject.toml --extra dev --universal -o requirements-dev.lock`, then install the result in a clean virtualenv and review changes. The universal locks cover platform markers; CI verifies Python 3.12 and 3.14. Architecture changes belong in a new ADR. Never edit applied migrations. No production data in issue bodies or test output.
