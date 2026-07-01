#!/usr/bin/env python3
"""
PM title filter — keep only product manager / senior PM / group PM variants.
Applies at both ingestion and display layer.
"""

import re

# Exact phrases that disqualify a title (case-insensitive, word-boundary match)
_EXCLUDE_PATTERNS = [
    r"\bvp\b",
    r"\bv\.p\.\b",
    r"\bvice president\b",
    r"\bdirector\b",
    r"\bhead of product\b",
    r"\bchief product\b",
    r"\bcpo\b",
    r"\bc\.p\.o\.\b",
    r"\bproduct operations\b",
    r"\bproduct ops\b",
    r"\bproduct operation\b",
    r"\bgrowth product\b",
    r"\bproduct designer\b",
    r"\bproduct design\b",
    r"\bproduct analyst\b",
    r"\bproduct marketing\b",
    r"\bproduct support\b",
    r"\bproduct enablement\b",
    r"\bdata product\b",
    r"\btechnical product\b",   # technical product manager is borderline — keeping for now
]

# Title must contain at least one of these to be considered a PM role
_INCLUDE_PATTERNS = [
    r"\bproduct manager\b",
    r"\bproduct owner\b",
    r"\bpm\b",
    r"\bsenior pm\b",
    r"\bsr\.?\s*pm\b",
    r"\bjunior pm\b",
    r"\bjr\.?\s*pm\b",
    r"\bgroup pm\b",
    r"\blead pm\b",
    r"\bprincipal pm\b",
    r"\bassociate pm\b",
    r"\bmanager, product\b",
    r"\bmanager of product\b",
]

_EXCL_RE = re.compile("|".join(_EXCLUDE_PATTERNS), re.IGNORECASE)
_INCL_RE = re.compile("|".join(_INCLUDE_PATTERNS), re.IGNORECASE)


def is_pm_title(title: str) -> bool:
    """Return True if title is a PM-level role (not VP/Director/CPO/Product Ops)."""
    if not title or not title.strip():
        return False
    t = title.strip()
    if _EXCL_RE.search(t):
        return False
    return bool(_INCL_RE.search(t))


def filter_jobs(jobs: list) -> list:
    """Filter a list of job dicts to only PM-level roles."""
    return [j for j in jobs if is_pm_title(j.get("title", "") or j.get("role", ""))]


if __name__ == "__main__":
    tests = [
        ("Product Manager", True),
        ("Senior Product Manager", True),
        ("Sr. Product Manager", True),
        ("Group Product Manager", True),
        ("Lead Product Manager", True),
        ("Junior Product Manager", True),
        ("Associate Product Manager", True),
        ("Product Owner", True),
        ("VP Product", False),
        ("Vice President Product", False),
        ("Director of Product", False),
        ("Head of Product", False),
        ("CPO", False),
        ("Chief Product Officer", False),
        ("Product Operations Manager", False),
        ("Product Ops Lead", False),
        ("Product Designer", False),
        ("Product Marketing Manager", False),
        ("Technical Product Manager", True),
    ]
    for title, expected in tests:
        result = is_pm_title(title)
        status = "✅" if result == expected else "❌"
        print(f"{status} {title!r:40s} → {result} (expected {expected})")
