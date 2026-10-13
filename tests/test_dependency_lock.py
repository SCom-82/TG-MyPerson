"""Dependency lock guard: the image is built strictly from requirements.lock.

Incident 07–08.10 (ro authz bypass): a rebuild without upper bounds pulled
FastAPI 0.141.1 into prod. Now pyproject.toml declares ranges, uv.lock holds the
resolution, requirements.lock (exported from uv.lock, with hashes) is what the
Dockerfile installs. These tests fail when the three drift apart — e.g. a
dependency added to pyproject.toml without `uv lock` + export. Update procedure:
README «Зависимости».
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _uv_lock() -> dict:
    return tomllib.loads((ROOT / "uv.lock").read_text())


def _requirements_lock() -> dict[str, str]:
    """name → pinned version; asserts every pin carries hashes."""
    pins: dict[str, str] = {}
    entries = re.split(r"\n(?=[A-Za-z0-9])", (ROOT / "requirements.lock").read_text())
    for entry in entries:
        if not entry.strip() or entry.lstrip().startswith("#"):
            continue
        head = entry.split("\\", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9._\-]+)(\[[^\]]*\])?==([^\s;]+)", head)
        assert m, f"requirements.lock: not an exact pin: {head!r}"
        assert "--hash=sha256:" in entry, f"requirements.lock: {head!r} has no hash"
        pins[canonicalize_name(m.group(1))] = m.group(3)
    return pins


def _project_requirements() -> list[Requirement]:
    return [Requirement(r) for r in _pyproject()["project"]["dependencies"]]


def test_every_runtime_dependency_is_locked_within_its_range():
    pins = _requirements_lock()
    for req in _project_requirements():
        name = canonicalize_name(req.name)
        assert name in pins, f"{req.name} is in pyproject.toml but not in requirements.lock"
        assert req.specifier.contains(pins[name], prereleases=True), (
            f"{req.name}=={pins[name]} in requirements.lock violates pyproject {req.specifier}"
        )


def test_uv_lock_matches_pyproject():
    """`uv lock` was rerun after the last pyproject.toml change."""
    project = next(p for p in _uv_lock()["package"] if p["name"] == "tg-myperson")
    locked = {
        (canonicalize_name(d["name"]), d.get("specifier", ""))
        for d in project["metadata"]["requires-dist"]
        if "marker" not in d
    }
    declared = {
        (canonicalize_name(r.name), str(r.specifier)) for r in _project_requirements()
    }

    def _norm(pairs):
        return {(n, ",".join(sorted(s.split(",")))) if s else (n, "") for n, s in pairs}

    assert _norm(locked) == _norm(declared), "pyproject.toml changed — run `uv lock`"


def test_requirements_lock_is_exported_from_uv_lock():
    versions = {canonicalize_name(p["name"]): p["version"] for p in _uv_lock()["package"]}
    for name, version in _requirements_lock().items():
        assert versions.get(name) == version, (
            f"{name}: requirements.lock {version} != uv.lock {versions.get(name)} — re-export"
        )


def test_guarded_pins():
    pins = _requirements_lock()
    major, minor, *_ = (int(x) for x in pins["fastapi"].split("."))
    assert (major, minor) < (0, 137), "FastAPI ≥0.137 breaks the authz route resolver"
    assert pins["maxapi-python"] == "2.4.1"


def test_dockerfile_installs_only_from_lock():
    dockerfile = (ROOT / "Dockerfile").read_text()
    installs = [line for line in dockerfile.splitlines() if "pip install" in line]
    assert installs, "Dockerfile has no pip install"
    for line in installs:
        locked = "--require-hashes" in line and "-r requirements.lock" in line
        project_only = "--no-deps" in line and re.search(r"pip install [^&]*--no-deps \.", line)
        assert locked or project_only, f"Dockerfile resolves dependencies outside the lock: {line}"
    assert "COPY requirements.lock" in dockerfile
    assert re.search(r"^FROM python:\d+\.\d+\.\d+-slim", dockerfile, re.M), (
        "base image must be pinned to a patch version"
    )
