"""scripts/build_release.py: allowlist, determinism, changelog/version gates."""

import hashlib
import importlib.util
import io
import os
import shutil
import zipfile

import pytest

from conftest import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "build_release", os.path.join(REPO_ROOT, "scripts", "build_release.py"))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


@pytest.fixture(scope="module")
def built():
    files = br.collect_files()
    return files, br.build_zip_bytes(files)


def test_zip_layout_and_allowlist(built):
    files, data = built
    shipped = br.self_check(data)
    assert shipped == files
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert all(n.startswith("PlasticityBridge/") for n in names)
    for required in br.REQUIRED:
        assert required in shipped
    forbidden = ("tests/", "scripts/", ".github/", "README.md", "CONTRIBUTING.md",
                 "AUDIT.md", "CHANGELOG.md", "requirements-dev.txt", "pytest.ini",
                 "speedups.c", "py.typed", ".pyc", "__pycache__", ".DS_Store",
                 "docs/", ".gitignore", ".coveragerc")
    for f in shipped:
        assert not any(x in f for x in forbidden), f
    # One private package; no generic top-level packages in the plugin folder.
    tops = {f.split("/", 1)[0] for f in shipped}
    assert tops == {"plasticity_c4d.pyp", "LICENSE", "readme.txt",
                    "plasticity_bridge", "res"}, tops
    assert "plasticity_bridge/_vendor/websockets/LICENSE" in shipped


def test_build_is_deterministic(built):
    files, data = built
    again = br.build_zip_bytes(br.collect_files())
    assert hashlib.sha256(again).digest() == hashlib.sha256(data).digest()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            mode = info.external_attr >> 16
            assert mode in (0o40755, 0o100644), (info.filename, oct(mode))


def test_crlf_checkouts_build_identical_zip(tmp_path, built):
    """A Windows checkout (CRLF text files) must give the same bytes."""
    files, data = built
    for rel in files:
        src = os.path.join(REPO_ROOT, *rel.split("/"))
        dst = tmp_path.joinpath(*rel.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw = open(src, "rb").read()
        if os.path.splitext(rel)[1] in (".py", ".pyp", ".h", ".res", ".str", ".txt", ""):
            raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        dst.write_bytes(raw)
    (tmp_path / "plasticity_bridge" / "core" / "__pycache__").mkdir()
    (tmp_path / "plasticity_bridge" / "core" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"x")
    (tmp_path / "res" / ".DS_Store").write_bytes(b"x")
    ws = tmp_path / "plasticity_bridge" / "_vendor" / "websockets"
    (ws / "speedups.c").write_bytes(b"x")
    (ws / "py.typed").write_bytes(b"")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "client.cpython-311.pyc").write_bytes(b"x")
    rebuilt = br.build_zip_bytes(br.collect_files(str(tmp_path)), str(tmp_path))
    assert rebuilt == data


def test_self_check_rejects_extra_and_missing_files(built):
    files, data = built
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as zf:
        zf.writestr("PlasticityBridge/tests/test_x.py", b"")
    with pytest.raises(br.BuildError, match="outside the allowlist"):
        br.self_check(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PlasticityBridge/plasticity_c4d.pyp", b"x = 1\n")
    with pytest.raises(br.BuildError, match="required files missing"):
        br.self_check(buf.getvalue())


def test_self_check_byte_compiles(built, monkeypatch):
    files, data = built
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            payload = src.read(info.filename)
            if info.filename.endswith("plasticity_bridge/core/handler.py"):
                payload += b"\ndef broken(:\n"
            dst.writestr(info, payload)
    with pytest.raises(br.BuildError,
                       match="syntax error in plasticity_bridge/core/handler.py"):
        br.self_check(buf.getvalue())


CHANGELOG = """# Changelog

## [Unreleased]

### Fixed
- something

## [1.1.6] - 2026-06-13

### Fixed
- older
"""


def _repo_with(tmp_path, changelog, version):
    (tmp_path / "plasticity_bridge" / "core").mkdir(parents=True)
    (tmp_path / "plasticity_bridge" / "core" / "version.py").write_text(
        f'__version__ = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return str(tmp_path)


def test_changelog_gates(tmp_path):
    root = _repo_with(tmp_path, CHANGELOG, "1.2.0")
    assert br.read_version(root) == "1.2.0"
    notes = br.release_notes("1.2.0", root=root)
    assert notes.startswith("## [1.2.0] - Unreleased") and "something" in notes
    with pytest.raises(br.BuildError, match="no '## \\[1.2.0\\]' section"):
        br.release_notes("1.2.0", release=True, root=root)
    with pytest.raises(br.BuildError, match="not newer than the latest release"):
        br.release_notes("1.1.5", root=root)
    notes = br.release_notes("1.1.6", release=True, root=root)
    assert notes.startswith("## [1.1.6] - 2026-06-13") and "older" in notes


def test_changelog_requires_content(tmp_path):
    root = _repo_with(tmp_path, CHANGELOG.replace("### Fixed\n- something\n", ""),
                      "1.2.0")
    with pytest.raises(br.BuildError, match="no entry for 1.2.0"):
        br.release_notes("1.2.0", root=root)
    dated = CHANGELOG.replace("## [Unreleased]", "## [1.2.0] - 2026-10-20")
    root = _repo_with(tmp_path / "b", dated, "1.2.0") if False else None
    sub = tmp_path / "b"
    sub.mkdir()
    root = _repo_with(sub, dated, "1.2.0")
    assert "something" in br.release_notes("1.2.0", release=True, root=root)


def test_version_must_be_semver(tmp_path):
    root = _repo_with(tmp_path, CHANGELOG, "1.2")
    with pytest.raises(br.BuildError, match="not X.Y.Z"):
        br.read_version(root)


def test_main_tag_mismatch_and_success(tmp_path, capsys):
    assert br.main(["--tag", "v0.0.1", "--out-dir", str(tmp_path)]) == 1
    assert "does not match" in capsys.readouterr().err
    version = br.read_version()
    notes = tmp_path / "notes.md"
    assert br.main(["--tag", f"v{version}", "--out-dir", str(tmp_path),
                    "--notes", str(notes), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert f"PlasticityBridge_v{version}.zip" in out and "sha256:" in out
    assert (tmp_path / f"PlasticityBridge_v{version}.zip").exists()
    assert notes.read_text().startswith(f"## [{version}]")
