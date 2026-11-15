import hashlib
import io
import os
import zipfile

from kbw import config, install, util


def test_find_rg_prefers_project_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    binary = tmp_path / "vendor/bin" / ("rg.exe" if os.name == "nt" else "rg")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"local")
    binary.chmod(0o755)
    monkeypatch.setattr(util, "resolve_command", lambda *_: ("PATH-rg", []))
    assert util.find_rg() == (str(binary), [])
    binary.unlink()
    assert util.find_rg() == ("PATH-rg", [])


def test_llmwiki_offline_never_runs_commands(tmp_path, monkeypatch):
    source = tmp_path / "llmwiki"
    entry = source / "packages/core/dist/mcp/bin.js"
    monkeypatch.setattr(install, "LLMWIKI_SOURCE", source)
    monkeypatch.setattr(install, "LLMWIKI_ENTRY", entry)
    def forbidden(*args, **kwargs):
        raise AssertionError("offline setup must not run clone/npm")
    assert not install.setup_llmwiki(True, None, forbidden)
    entry.parent.mkdir(parents=True)
    entry.write_text("built")
    (source / "node_modules").mkdir()
    assert install.setup_llmwiki(True, None, forbidden)


def test_llmwiki_builds_pinned_core_only(tmp_path, monkeypatch):
    source = tmp_path / "llmwiki"
    entry = source / "packages/core/dist/mcp/bin.js"
    monkeypatch.setattr(install, "LLMWIKI_SOURCE", source)
    monkeypatch.setattr(install, "LLMWIKI_ENTRY", entry)
    calls = []
    def stream(cmd, **kw):
        calls.append((cmd, kw))
        if "build" in cmd:
            entry.parent.mkdir(parents=True)
            entry.write_text("built")
        return 0
    assert install.setup_llmwiki(False, ("npm", []), stream)
    assert calls[0][0][:3] == ["git", "clone", "https://github.com/microsoft/llmwiki.git"]
    assert calls[1][0][-2:] == ["checkout", config.UPSTREAM["llmwiki_commit"]]
    assert calls[2][0][1] == "ci"
    assert "--workspace=packages/core" in calls[3][0]


def test_rg_offline_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    def forbidden(*args):
        raise AssertionError("offline setup must not download")
    monkeypatch.setattr(install, "_download", forbidden)
    assert not install.setup_rg(True)


def test_rg_archive_checksum_and_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(install, "rg_target", lambda: ("x86_64-pc-windows-msvc", "zip"))
    exe = "rg.exe" if os.name == "nt" else "rg"
    stem = f"ripgrep-{config.UPSTREAM['ripgrep']}-x86_64-pc-windows-msvc"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr(f"{stem}/{exe}", b"verified binary")
        z.writestr("../../escape", b"must not extract")
    archive = data.getvalue()
    valid = False
    def download(url, dest):
        if url.endswith(".sha256"):
            digest = hashlib.sha256(archive).hexdigest() if valid else "0" * 64
            dest.write_text(f"SHA256 hash of {stem}.zip:\n{digest}\nCertUtil: completed.\n")
        else:
            dest.write_bytes(archive)
    monkeypatch.setattr(install, "_download", download)
    assert not install.setup_rg(False)
    assert not (tmp_path / "vendor/bin" / exe).exists()
    valid = True
    assert install.setup_rg(False)
    assert (tmp_path / "vendor/bin" / exe).read_bytes() == b"verified binary"
    assert not (tmp_path / "escape").exists()


def test_rg_linux_x64_target(monkeypatch):
    monkeypatch.setattr(install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(install.sys, "platform", "linux")
    assert install.rg_target() == ("x86_64-unknown-linux-musl", "tar.gz")
