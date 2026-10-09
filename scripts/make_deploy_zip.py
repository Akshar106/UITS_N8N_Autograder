#!/usr/bin/env python3
"""Build app.zip with the application in it and nothing else.

    python3 scripts/make_deploy_zip.py

Zipping the project folder sweeps up whatever is sitting in it. Three of those
things matter:

  .env          the IU and Canvas client secrets, the Flask signing key and the
                LLM API key. Secrets belong in Application Settings; a copy on
                the server is a copy that can leak and that nobody rotates.
  instance/     the local SQLite database -- Canvas tokens and every grade and
                approval from local testing, uploaded to a live host.
  .venv/        hundreds of megabytes of local virtualenv, built for the wrong
                platform, which Oryx rebuilds from requirements.txt anyway.

So this ships an explicit list rather than excluding a blacklist: anything not
named here does not go, which fails safe when a new file appears in the root.

The result is byte-identical to what the app needs and nothing more; Oryx
installs requirements.txt on the Azure side as it does today.
"""
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "app.zip"

# Directories shipped whole, minus the exclusions below.
DIRECTORIES = ["app"]

# Individual files at the repository root.
FILES = ["requirements.txt", "run.py", ".deployment"]

# Google Search Console's verification file is served by the app and has to be
# on the server for the domain to stay verified, which is what allows a Safe
# Browsing review to be requested at all.
ROOT_GLOBS = ["google*.html"]

# Never shipped, wherever they appear.
EXCLUDED_DIRS = {"__pycache__", ".venv", "venv", ".git", "instance", "node_modules"}
EXCLUDED_NAMES = {".env", ".DS_Store", "app.zip"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite3", ".zip", ".log"}


def _wanted(path):
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    if path.name in EXCLUDED_NAMES:
        return False
    return path.suffix not in EXCLUDED_SUFFIXES


def collect():
    members = []
    for directory in DIRECTORIES:
        root = REPO_ROOT / directory
        if not root.is_dir():
            sys.exit(f"missing directory: {directory}")
        members += [p for p in sorted(root.rglob("*")) if p.is_file() and _wanted(p)]
    for name in FILES:
        path = REPO_ROOT / name
        if path.is_file():
            members.append(path)
        elif name != ".deployment":
            sys.exit(f"missing file: {name}")
    for pattern in ROOT_GLOBS:
        members += [p for p in sorted(REPO_ROOT.glob(pattern)) if p.is_file()]
    return members


def stamp_version():
    """Write the commit being packaged so /healthz can report it.

    Diagnosing this deployment has repeatedly stalled on not knowing whether a
    fix had actually shipped. A build that cannot name itself makes every
    symptom ambiguous.
    """
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha, dirty = "unknown", ""
    version = sha + ("+local-changes" if dirty else "")
    (REPO_ROOT / "app" / "_version.py").write_text(
        '"""Written by scripts/make_deploy_zip.py. Not edited by hand."""\n'
        f'VERSION = "{version}"\n')
    return version


def main():
    version = stamp_version()
    members = collect()

    # A last check rather than a comment: the point of the whitelist is that
    # these can never be in the archive, so say so out loud if one ever is.
    for path in members:
        assert path.name != ".env", "refusing to package .env"
        assert path.suffix != ".sqlite3", f"refusing to package a database: {path}"

    if TARGET.exists():
        TARGET.unlink()
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(REPO_ROOT))

    size = TARGET.stat().st_size
    print(f"{TARGET.relative_to(REPO_ROOT)}: {len(members)} files, {size // 1024} KB, "
          f"version {version}")
    print("  excluded: .env, instance/, .venv/, __pycache__/, *.pyc, *.sqlite3")
    print("\nDeploy it with:")
    print("  az webapp deploy --resource-group <rg> --name <app> "
          "--src-path app.zip --type zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
