"""Private credentials files for out-of-band token distribution.

Tokens are written **outside** the media tree (never web-served), one file
per election, and are downloadable only by authorized officials through an
audited view.  Only the voter's identifier and token are written - the file
is meant to be printed/cut and distributed by the institution, then purged.
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.conf import settings


def credentials_dir() -> Path:
    root = Path(settings.BASE_DIR) / "private" / "credentials"
    root.mkdir(parents=True, exist_ok=True)
    return root


def credentials_path(election) -> Path:
    return credentials_dir() / f"{election.pk}.csv"


def append_credentials(election, rows: list[tuple[str, str, str]]) -> Path:
    """Append ``(voter_id, token, full_name)`` rows to the election file."""
    path = credentials_path(election)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["voter_id", "token", "full_name"])
        for voter_id, token, full_name in rows:
            writer.writerow([voter_id, token, full_name])
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return path


def purge_credentials(election) -> bool:
    path = credentials_path(election)
    if path.exists():
        path.unlink()
        return True
    return False
