#!/usr/bin/env python3
"""
Build the Plasticity Bridge release zip.

    python scripts/build_release.py                    # dist/PlasticityBridge_v<version>.zip
    python scripts/build_release.py --release --tag v1.2.0 --notes dist/RELEASE_NOTES.md

Standard library only; works on Windows, macOS and Linux.

* The version comes from plasticity_bridge/core/version.py (single source
  of truth).
* CHANGELOG.md must describe that version: a ``## [<version>]`` section, or
  — for pre-release builds only — a non-empty ``## [Unreleased]`` section
  while <version> is newer than every released entry.  ``--release`` (used
  by the release workflow) requires a dated ``## [<version>] - YYYY-MM-DD``
  section and, with ``--tag``, that the tag is ``v<version>``.
* The zip holds exactly one top-level folder, ``PlasticityBridge/`` (same
  layout as the v1.1.x releases, so unzipping over an old install
  replaces it), filled from an explicit allowlist.
* Output is deterministic: sorted entries, fixed timestamps, normalised
  permissions, forward-slash paths, LF line endings for text files.  The
  zip is built twice and both builds must be byte-identical.
* Self-check: lists the contents, fails on anything outside the allowlist or
  any missing required file, byte-compiles every Python file (in memory —
  no .pyc is written or shipped), prints size and SHA-256.
"""

import argparse
import ast
import fnmatch
import hashlib
import io
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP_FOLDER = "PlasticityBridge"
ZIP_NAME = "PlasticityBridge_v{version}.zip"
VERSION_FILE = "plasticity_bridge/core/version.py"

# (directory relative to the repo root, glob patterns, recurse?)
# All plugin code is in the private package plasticity_bridge/ (vendored
# libraries under plasticity_bridge/_vendor/); nothing else is a package.
ALLOWLIST = [
    ("", ["plasticity_c4d.pyp", "LICENSE", "readme.txt"], False),
    ("plasticity_bridge", ["*.py"], True),
    ("plasticity_bridge/_vendor/websockets", ["LICENSE"], False),
    ("res", ["*.h", "*.res", "*.str", "*.tif"], True),
]

_PKG = "plasticity_bridge/"
_WS = _PKG + "_vendor/websockets/"
REQUIRED = [
    "plasticity_c4d.pyp", "LICENSE", "readme.txt",
    _PKG + "__init__.py", _PKG + "_vendor/__init__.py",
    _PKG + "core/__init__.py", _PKG + "core/client.py",
    _PKG + "core/geometry.py", _PKG + "core/handler.py",
    _PKG + "core/protocol.py", _PKG + "core/refacet_settings.py",
    _PKG + "core/refacet_tag.py", _PKG + "core/threading_bridge.py",
    VERSION_FILE,
    _PKG + "dialogs/__init__.py", _PKG + "dialogs/main_dialog.py",
    _WS + "__init__.py", _WS + "client.py", _WS + "legacy/client.py",
    _WS + "LICENSE",
    "res/c4d_symbols.h", "res/icon.tif",
    "res/description/tplasticityrefacet.h",
    "res/description/tplasticityrefacet.res",
    "res/strings_us/c4d_strings.str",
    "res/strings_us/description/tplasticityrefacet.str",
]

# Never shipped, even if a pattern above would match (belt and braces).
NEVER = ["*/__pycache__/*", "*.pyc", "*.pyo", "*/.DS_Store", "*Thumbs.db",
         "*/speedups.c", "*/py.typed", "*.pyi"]

TEXT_EXTENSIONS = {".py", ".pyp", ".h", ".res", ".str", ".txt", ""}
FIXED_DATE = (1980, 1, 1, 0, 0, 0)      # earliest date the zip format allows


class BuildError(Exception):
    pass


# =============================================================================
# Version and changelog
# =============================================================================

def read_version(root=ROOT):
    path = os.path.join(root, *VERSION_FILE.split("/"))
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "__version__"):
            version = ast.literal_eval(node.value)
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise BuildError(f"{VERSION_FILE}: {version!r} is not X.Y.Z")
            return version
    raise BuildError(f"{VERSION_FILE} has no __version__ = '...' assignment")


_HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?P<rest>.*)$", re.M)


def changelog_sections(text):
    """[(name, rest_of_heading, body)] for every ``## [...]`` section."""
    matches = list(_HEADING.finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group("name").strip(), m.group("rest").strip(),
                    text[m.end():end].strip()))
    return out


def _vtuple(v):
    return tuple(int(x) for x in v.split("."))


def release_notes(version, release=False, root=ROOT):
    """Check CHANGELOG.md for ``version``; return the release-notes text."""
    path = os.path.join(root, "CHANGELOG.md")
    with open(path, encoding="utf-8") as f:
        sections = changelog_sections(f.read())
    by_name = {name: (rest, body) for name, rest, body in sections}

    if version in by_name:
        rest, body = by_name[version]
        if release and not re.search(r"\d{4}-\d{2}-\d{2}", rest):
            raise BuildError(f"CHANGELOG.md: '## [{version}]' needs a release "
                             f"date ('## [{version}] - YYYY-MM-DD')")
        if not body:
            raise BuildError(f"CHANGELOG.md: section [{version}] is empty")
        return f"## [{version}] {rest}".rstrip() + "\n\n" + body + "\n"

    if release:
        raise BuildError(f"CHANGELOG.md has no '## [{version}]' section — rename "
                         f"'## [Unreleased]' to '## [{version}] - YYYY-MM-DD' "
                         f"before tagging")
    released = [n for n in by_name if re.fullmatch(r"\d+\.\d+\.\d+", n)]
    newest = max(released, key=_vtuple) if released else None
    unreleased = by_name.get("Unreleased", ("", ""))[1]
    if not unreleased:
        raise BuildError(f"CHANGELOG.md has no entry for {version}: add a "
                         f"'## [{version}]' section or describe the changes "
                         f"under '## [Unreleased]'")
    if newest and _vtuple(version) <= _vtuple(newest):
        raise BuildError(f"CHANGELOG.md has no '## [{version}]' section and "
                         f"{version} is not newer than the latest release "
                         f"({newest}) — bump {VERSION_FILE}")
    print(f"note: {version} is not released yet; release notes come from "
          f"'## [Unreleased]' (use --release to require a dated entry)")
    return f"## [{version}] - Unreleased\n\n{unreleased}\n"


# =============================================================================
# File selection
# =============================================================================

def _excluded(rel):
    probe = "/" + rel
    return any(fnmatch.fnmatch(probe, pat) or fnmatch.fnmatch(rel, pat)
               for pat in NEVER)


def allowed(rel):
    """True if a repo-relative path (forward slashes) is on the allowlist."""
    if _excluded(rel):
        return False
    directory, _, name = rel.rpartition("/")
    for base, patterns, recurse in ALLOWLIST:
        if directory == base or (recurse and directory.startswith(base + "/")):
            if any(fnmatch.fnmatchcase(name, p) for p in patterns):
                return True
    return False


def collect_files(root=ROOT):
    """Sorted repo-relative paths of every allowlisted file."""
    files = []
    for base, patterns, recurse in ALLOWLIST:
        start = os.path.join(root, *base.split("/")) if base else root
        if not os.path.isdir(start):
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            if not recurse:
                dirnames[:] = []
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                rel = rel.replace(os.sep, "/")
                if allowed(rel):
                    files.append(rel)
    return sorted(set(files))


def read_payload(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as f:
        data = f.read()
    if os.path.splitext(rel)[1].lower() in TEXT_EXTENSIONS:
        data = data.replace(b"\r\n", b"\n")
    return data


# =============================================================================
# Zip
# =============================================================================

def _zipinfo(name, is_dir):
    zi = zipfile.ZipInfo(name, date_time=FIXED_DATE)
    zi.create_system = 3                     # Unix, on every host OS
    if is_dir:
        zi.external_attr = (0o40755 << 16) | 0x10
        zi.compress_type = zipfile.ZIP_STORED
    else:
        zi.external_attr = 0o100644 << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def build_zip_bytes(files, root=ROOT):
    entries = {}
    for rel in files:
        entries[f"{TOP_FOLDER}/{rel}"] = read_payload(root, rel)
    dirs = {f"{TOP_FOLDER}/"}
    for name in entries:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]) + "/")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(dirs | set(entries)):
            if name.endswith("/"):
                zf.writestr(_zipinfo(name, True), b"")
            else:
                zf.writestr(_zipinfo(name, False), entries[name],
                            compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def self_check(data):
    """Validate a built zip; returns the list of file entries."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        bad = [n for n in names if "\\" in n or n.startswith("/")]
        if bad:
            raise BuildError(f"non-portable paths: {bad}")
        tops = {n.split("/", 1)[0] for n in names}
        if tops != {TOP_FOLDER}:
            raise BuildError(f"expected one top-level folder {TOP_FOLDER}/, "
                             f"found {sorted(tops)}")
        if names != sorted(names):
            raise BuildError("zip entries are not sorted")
        files = [n[len(TOP_FOLDER) + 1:] for n in names if not n.endswith("/")]
        outside = [f for f in files if not allowed(f)]
        if outside:
            raise BuildError(f"files outside the allowlist: {outside}")
        missing = [f for f in REQUIRED if f not in files]
        if missing:
            raise BuildError(f"required files missing: {missing}")
        for info in zf.infolist():
            if info.date_time != FIXED_DATE:
                raise BuildError(f"{info.filename}: unexpected timestamp")
        for f in files:
            if f.endswith((".py", ".pyp")):
                source = zf.read(f"{TOP_FOLDER}/{f}")
                try:
                    compile(source, f"{TOP_FOLDER}/{f}", "exec",
                            dont_inherit=True)
                except SyntaxError as e:
                    raise BuildError(f"syntax error in {f}: {e}") from None
    return files


# =============================================================================
# Main
# =============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--release", action="store_true",
                    help="require a dated CHANGELOG entry for the version")
    ap.add_argument("--tag", help="fail unless this git tag is v<version>")
    ap.add_argument("--notes", help="write the version's CHANGELOG section here")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--quiet", action="store_true", help="don't list contents")
    args = ap.parse_args(argv)

    try:
        version = read_version()
        if args.tag is not None and args.tag != f"v{version}":
            raise BuildError(f"tag {args.tag!r} does not match {VERSION_FILE} "
                             f"(expected 'v{version}')")
        notes = release_notes(version, release=args.release)

        files = collect_files()
        data = build_zip_bytes(files)
        if build_zip_bytes(collect_files()) != data:
            raise BuildError("two consecutive builds differ — output is not "
                             "deterministic")
        shipped = self_check(data)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, ZIP_NAME.format(version=version))
    with open(out, "wb") as f:
        f.write(data)
    if args.notes:
        os.makedirs(os.path.dirname(os.path.abspath(args.notes)), exist_ok=True)
        with open(args.notes, "w", encoding="utf-8", newline="\n") as f:
            f.write(notes)

    if not args.quiet:
        print(f"Contents of {os.path.basename(out)}:")
        for f in shipped:
            print(f"  {TOP_FOLDER}/{f}")
    print(f"{len(shipped)} files, all Python files compile")
    try:
        shown = os.path.relpath(out, ROOT)
    except ValueError:                       # other drive on Windows
        shown = out
    print(f"zip:    {shown}")
    print(f"size:   {len(data)} bytes")
    print(f"sha256: {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
