"""The previous LLM provider must be gone from code, dependencies and CI config."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY = "anth" + "ropic"  # split so this file doesn't match itself
CHECKED = ["*.py", "newsletter/*.py", "requirements*.txt", ".github/workflows/*.yml", "README.md", ".env.example", "config/*.toml"]


def test_no_legacy_provider_references():
    hits = []
    for pattern in CHECKED:
        for path in ROOT.glob(pattern):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if LEGACY in line.lower() or "claude" in line.lower():
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == []


def test_requirements_list_openai_sdk_only():
    reqs = (ROOT / "requirements.txt").read_text().split()
    assert "openai" in reqs and LEGACY not in reqs
