"""The Vercel API bundle (P2 review fix A3): agents/prompts/ ships, other bulk stays out."""

import json
import re
from fnmatch import fnmatch
from pathlib import Path


def _expand(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [p for part in match.group(1).split(",") for p in _expand(head + part + tail)]


def _vercelignored(path: str, lines: list[str]) -> bool:
    """gitignore semantics for the patterns used in .vercelignore (last match wins)."""
    ignored = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        if pattern.endswith("/"):
            hit = path.startswith(pattern) or any(
                fnmatch(prefix + "/", pattern)
                for prefix in ("/".join(path.split("/")[: i + 1]) for i in range(path.count("/")))
            )
        elif "/" in pattern:
            hit = fnmatch(path, pattern)
        else:
            hit = fnmatch(path.rsplit("/", 1)[-1], pattern)
        if hit:
            ignored = not negate
    return ignored


def test_prompts_ship_in_the_api_bundle():
    lines = Path(".vercelignore").read_text().splitlines()
    excluded = _expand(
        json.loads(Path("vercel.json").read_text())["functions"]["index.py"]["excludeFiles"]
    )
    prompts = sorted(str(p) for p in Path("agents/prompts").glob("*.md"))
    assert "agents/prompts/receptionist.md" in prompts
    for path in prompts:
        assert not _vercelignored(path, lines), path
        assert not any(fnmatch(path, pattern) for pattern in excluded), path
    for path in (
        "agents/scenarios/acceptance.json",
        "agents/README.md",
        "docs/API.md",
        "tests/conftest.py",
        "web/package.json",
        "README.md",
    ):
        assert _vercelignored(path, lines) or any(fnmatch(path, p) for p in excluded), path
    assert _vercelignored("agents/flow.yaml", lines)
