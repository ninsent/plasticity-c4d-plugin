"""Documentation consistency: every manual check cited in AUDIT.md exists."""

import os
import re

from conftest import REPO_ROOT

AUDIT = os.path.join(REPO_ROOT, "AUDIT.md")
CHECKLIST = os.path.join(REPO_ROOT, "docs", "MANUAL_TEST_CHECKLIST.md")

_CITED = re.compile(r"\bM-([A-Z]+)-(\d+|\*)")
_DEFINED = re.compile(r"^\|\s*(M-[A-Z]+-\d+)\s*\|", re.M)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def checklist_ids():
    ids = _DEFINED.findall(_read(CHECKLIST))
    assert len(ids) == len(set(ids)), "duplicate check IDs in the checklist"
    return set(ids)


def test_every_check_cited_in_audit_exists():
    defined = checklist_ids()
    missing = []
    for area, num in set(_CITED.findall(_read(AUDIT))):
        if num == "*":
            if not any(i.startswith(f"M-{area}-") for i in defined):
                missing.append(f"M-{area}-*")
        elif f"M-{area}-{num}" not in defined:
            missing.append(f"M-{area}-{num}")
    assert not missing, f"cited in AUDIT.md but not in the checklist: {sorted(missing)}"


def test_checklist_has_setup_section_and_mcp_check():
    text = _read(CHECKLIST)
    assert re.search(r"^## Setup\b", text, re.M)       # AUDIT cites "the setup section"
    assert "M-MCP-1" in checklist_ids()
    assert "[`docs/MANUAL_TEST_CHECKLIST.md`](docs/MANUAL_TEST_CHECKLIST.md)" in _read(AUDIT)
