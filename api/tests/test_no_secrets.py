"""Guard that makes a public repo safe: nothing resembling a live credential
may sit in any file git knows about.

This repo is public from commit one. A leaked key is ROTATED, never merely
deleted: git history is forever, and public history is scraped by bots within
minutes of a push, so removing the line in a later commit protects nothing.
This test is meant to fail before the key ever lands, so it deliberately scans
untracked-but-not-ignored files as well as tracked ones (see
``_candidate_files``); do not narrow it back to tracked-only. Offline, no
network.
"""

import os
import re
import subprocess
from pathlib import Path

# Patterns from the task spec. Compiled at import so a typo fails loudly.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    "Hugging Face token": re.compile(r"hf_[A-Za-z0-9]{20,}"),
    "Stripe/Clerk-style secret key": re.compile(r"sk_(live|test)_[A-Za-z0-9]{10,}"),
    "PEM private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Anything larger is media or a generated blob, not source a human pasted into.
MAX_BYTES = 1_000_000

THIS_FILE = Path(__file__).resolve()
# The template is all empty values and comments by contract; a real value
# there is caught by review, and scanning it would only produce false hits
# on the placeholder names.
SKIP_NAMES = {".env.example"}


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=THIS_FILE.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def _candidate_files(root: Path) -> list[Path]:
    # --cached lists tracked files; --others --exclude-standard adds untracked
    # files that .gitignore does not cover. Scanning both means a pasted key is
    # caught before it is even staged, not only after `git add`.
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [root / os.fsdecode(raw) for raw in out.stdout.split(b"\0") if raw]


def _read_text(path: Path) -> str | None:
    """Return the file's text, or None if it should be skipped."""
    if not path.is_file():
        return None  # deleted-but-still-tracked, or a submodule directory
    if path.resolve() == THIS_FILE or path.name in SKIP_NAMES:
        return None
    if path.stat().st_size > MAX_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None  # binary
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_no_secrets_in_repo():
    root = _repo_root()
    hits: list[str] = []
    scanned = 0
    for path in _candidate_files(root):
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(root)
                hits.append(f"{rel}:{line}: looks like a {label} (pattern {pattern.pattern})")

    assert scanned > 0, "git ls-files returned nothing to scan; is this running inside the repo?"
    assert not hits, (
        "Possible secret(s) in the working tree. Rotate them at the provider now; "
        "deleting the line does not undo a push:\n  " + "\n  ".join(hits)
    )
