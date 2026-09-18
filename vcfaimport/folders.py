"""Folder-path matching, shared by discovery (selection) and the run state (execution)."""

from __future__ import annotations

import fnmatch
from typing import Sequence


def norm_folder(value: str) -> str:
    """Folder paths compare case-insensitively, with either slash, no edge slashes."""
    return (value or "").replace("\\", "/").strip("/").lower()


def folder_matches(path: str, patterns: Sequence[str], exact: bool = False) -> bool:
    """Does a VM's folder path fall under any of the given folders?

    A plain folder matches itself and, unless `exact`, everything beneath it:
    "Production" matches "Production" and "Production/Web/Tier1". A pattern
    with wildcards is a glob over the whole path. A lone "/" (or "") means the
    datacenter root folder itself.
    """
    have = norm_folder(path)
    for raw in patterns:
        want = norm_folder(raw)
        if any(ch in want for ch in "*?["):
            if fnmatch.fnmatch(have, want):
                return True
            continue
        if have == want:
            return True
        if not exact and want and have.startswith(want + "/"):
            return True
        if not exact and not want and raw.strip() in ("/", ""):
            return True   # root folder pattern with recursion == everything
    return False


def describe(patterns: Sequence[str], exact: bool = False) -> str:
    """Human-readable scope label for log lines."""
    if not patterns:
        return ""
    label = ", ".join(p if p.strip() else "/" for p in patterns)
    return "folder {}{}".format(label, " (no subfolders)" if exact else " (with subfolders)")
