"""Project-local installers, called only by setup. Downloads use the stdlib."""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from .config import LLMWIKI_ENTRY, LLMWIKI_SOURCE, PROJECT_ROOT, UPSTREAM, downstream_env


def setup_llmwiki(offline: bool, npm, stream) -> bool:
    if offline:
        ok = LLMWIKI_ENTRY.is_file() and (LLMWIKI_SOURCE / "node_modules").is_dir()
        if not ok:
            print("  LLMWiki missing/unbuilt: copy vendor/src/llmwiki (including node_modules and "
                  "packages/core/dist) from an online machine; offline setup never clones or builds.")
        return ok
    if not npm:
        print("  LLMWiki source build requires npm; re-run setup after installing Node.js.")
        return False
    env = downstream_env()
    cache = PROJECT_ROOT / "vendor" / "npm-cache"
    if not LLMWIKI_SOURCE.exists():
        LLMWIKI_SOURCE.parent.mkdir(parents=True, exist_ok=True)
        if stream(["git", "clone", "https://github.com/microsoft/llmwiki.git", str(LLMWIKI_SOURCE)], env=env):
            return False
    if stream(["git", "-C", str(LLMWIKI_SOURCE), "checkout", UPSTREAM["llmwiki_commit"]], env=env):
        return False
    for args in (["ci", "--cache", str(cache), "--no-audit", "--no-fund"],
                 ["run", "build", "--workspace=packages/core", "--cache", str(cache)]):
        if stream([npm[0], *npm[1], *args], cwd=LLMWIKI_SOURCE, env=env):
            return False
    if not LLMWIKI_ENTRY.is_file():
        print(f"  LLMWiki build did not produce {LLMWIKI_ENTRY}")
        return False
    return True


def rg_target() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine not in ("amd64", "x86_64"):
        raise ValueError(f"ripgrep local installer does not support {platform.system()} {machine}")
    if sys.platform == "win32":
        return "x86_64-pc-windows-msvc", "zip"
    if sys.platform == "linux":
        return "x86_64-unknown-linux-musl", "tar.gz"
    raise ValueError(f"ripgrep local installer does not support {sys.platform}")


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "kb-workbench-setup"})
    with urllib.request.urlopen(request, timeout=120) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)


def setup_rg(offline: bool) -> bool:
    executable = "rg.exe" if os.name == "nt" else "rg"
    dest = PROJECT_ROOT / "vendor" / "bin" / executable
    if dest.is_file():
        return True
    if offline:
        print(f"  ripgrep missing: copy vendor/bin/{executable} from an online machine; offline setup never downloads.")
        return False
    try:
        target, suffix = rg_target()
        stem = f"ripgrep-{UPSTREAM['ripgrep']}-{target}"
        name = f"{stem}.{suffix}"
        cache = PROJECT_ROOT / "vendor" / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        archive, checksum = cache / name, cache / (name + ".sha256")
        base = f"https://github.com/BurntSushi/ripgrep/releases/download/{UPSTREAM['ripgrep']}/"
        _download(base + name, archive)
        _download(base + name + ".sha256", checksum)
        # Official Windows files include CertUtil prose around the digest.
        hashes = re.findall(r"\b[0-9a-fA-F]{64}\b", checksum.read_text(encoding="utf-8"))
        if len(hashes) != 1:
            raise ValueError("ripgrep checksum file must contain exactly one SHA256 digest")
        expected = hashes[0].lower()
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if len(expected) != 64 or actual != expected:
            raise ValueError(f"ripgrep SHA256 mismatch: expected {expected}, got {actual}")
        print(f"  SHA256 verified: {actual}")
        # Extract only this regular executable; no archive paths are written.
        member = f"{stem}/{executable}"
        if suffix == "zip":
            with zipfile.ZipFile(archive) as z:
                data = z.read(member)
        else:
            with tarfile.open(archive, "r:gz") as t:
                info = t.getmember(member)
                if not info.isfile():
                    raise ValueError("ripgrep archive executable is not a regular file")
                with t.extractfile(info) as f:
                    data = f.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o755)
        return True
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"  ripgrep install failed: {type(exc).__name__}: {exc}")
        return False
