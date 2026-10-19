"""Guard for a public repo: no secret value or email address from your local .env may appear
in any file git would commit (tracked, or new and not ignored).

Runs only where a real .env exists (your machine). Anyone who clones the repo has no .env,
so it skips for them.
"""

import re
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
SECRET_NAME = re.compile(r"KEY|PASSWORD|TOKEN|SECRET", re.I)


def committable_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git repository")
    return [ROOT / name for name in out.split("\0") if name]


def private_values() -> dict[str, str]:
    if not ENV.exists():
        pytest.skip("no local .env (nothing private to check)")
    values = {}
    for name, value in dotenv_values(ENV).items():
        value = (value or "").strip()
        if SECRET_NAME.search(name) and len(value) >= 8:
            values[name] = value
        for address in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", value):
            values[f"{name} (email address)"] = address
    return values


def test_private_values_never_reach_a_committed_file():
    private = private_values()
    leaks = []
    for path in committable_files():
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        leaks += [f"{path.relative_to(ROOT)}: {name}" for name, value in private.items() if value in text]
    assert leaks == [], "Private values would be committed:\n" + "\n".join(leaks)


def test_secret_files_are_ignored():
    committed = {p.relative_to(ROOT).as_posix() for p in committable_files()}
    for name in (".env", ".env.backup", "newsletter_history.json", "preview.html"):
        assert name not in committed, f"{name} would be committed"
    assert ".env.example" in committed
