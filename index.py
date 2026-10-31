"""Vercel entrypoint for the API project (repository root; ADR 0010).

Vercel's Python runtime loads the top-level `app` from this file (pyproject.toml
[tool.vercel] entrypoint = "index:app"; vercel.json configures the function). The package
lives under src/, so it is put on the path in case the build installed only the
dependencies and not the project itself. Nothing runs at import beyond building the app:
migrations are never applied at cold start (operators run `clinic-voice migrate`).
"""

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "src"
if SOURCE.is_dir() and str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from clinic_voice.api import app  # noqa: E402

__all__ = ["app"]
