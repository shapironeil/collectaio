"""In-app updater: compare the installed commit with the GitHub branch, download the zipball,
sync the code (never touching personal data), reinstall requirements, then restart.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from drop_monitor import __version__

log = logging.getLogger(__name__)
REPO = "shapironeil/collectaio"
DEFAULT_BRANCH = "claude/quirky-ramanujan-1kvhs9"
PRESERVE_DIRS = {".git", ".venv", "data", "personal", "_backup", "portable", "_tmp", "__pycache__"}
PRESERVE_FILES = {"config.yaml", ".env", "setup.local.bat", "install-info.txt", "avvia.bat"}


def install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_install_info(root: Path) -> dict:
    info = {}
    p = root / "install-info.txt"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    return info


def write_install_info(root: Path, **fields) -> None:
    info = read_install_info(root)
    info.update({k: str(v) for k, v in fields.items()})
    (root / "install-info.txt").write_text("".join(f"{k}={v}\n" for k, v in info.items()), encoding="utf-8")


class Updater:
    def __init__(self, root: Path | None = None, token: str | None = None):
        self.root = root or install_root()
        info = read_install_info(self.root)
        self.branch = os.environ.get("DROP_MONITOR_BRANCH") or info.get("branch") or DEFAULT_BRANCH
        self.installed_commit = info.get("commit", "-")
        self.token = token or os.environ.get("GITHUB_TOKEN") or ""
        headers = {"User-Agent": "collectaio-updater", "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(headers=headers, timeout=httpx.Timeout(60), follow_redirects=True)

    def check(self) -> dict:
        r = self._client.get(f"https://api.github.com/repos/{REPO}/commits/{self.branch}")
        if r.status_code != 200:
            return {"ok": False, "error": f"GitHub HTTP {r.status_code}", "installed": self.installed_commit, "version": __version__, "branch": self.branch}
        j = r.json()
        sha = j.get("sha", "")[:7]
        rv = self._client.get(f"https://api.github.com/repos/{REPO}/contents/drop_monitor/__init__.py",
                              params={"ref": self.branch}, headers={"Accept": "application/vnd.github.raw"})
        m = re.search(r'__version__\s*=\s*"([^"]+)"', rv.text) if rv.status_code == 200 else None
        remote_version = m.group(1) if m else "?"
        return {
            "ok": True, "branch": self.branch, "installed": self.installed_commit, "latest": sha,
            "version": __version__, "remote_version": remote_version,
            "update_available": bool(sha) and sha != self.installed_commit[:7],
            "message": (j.get("commit", {}).get("message") or "").split("\n")[0][:120],
            "date": (j.get("commit", {}).get("committer") or {}).get("date", ""),
        }

    def apply(self, install_requirements: bool = True) -> dict:
        info = self.check()
        if not info.get("ok"):
            return info
        r = self._client.get(f"https://api.github.com/repos/{REPO}/zipball/{self.branch}")
        if r.status_code != 200 or len(r.content) < 10_000:
            return {"ok": False, "error": f"download zip fallito (HTTP {r.status_code})"}
        backup = self.root / "_backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        for f in PRESERVE_FILES:
            if (self.root / f).is_file():
                shutil.copy2(self.root / f, backup / f)
        with tempfile.TemporaryDirectory(dir=str(self.root / "_backup")) as tmp:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                z.extractall(tmp)
            src = next(Path(tmp).iterdir())
            changed = _sync_tree(src, self.root)
        write_install_info(self.root, commit=info["latest"], branch=self.branch, app_version=info.get("remote_version", __version__),
                           updated_at=datetime.now().isoformat(timespec="seconds"))
        pip_out = ""
        if install_requirements and (self.root / "requirements.txt").is_file():
            cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-warn-script-location", "-r", str(self.root / "requirements.txt")]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(self.root))
                pip_out = (res.stdout + res.stderr)[-800:]
                if res.returncode != 0:
                    return {"ok": False, "error": "aggiornamento dipendenze fallito", "detail": pip_out, "changed": changed}
            except Exception as e:  # pragma: no cover
                return {"ok": False, "error": f"pip: {e}", "changed": changed}
        log.info("updated to %s (%d files changed)", info["latest"], changed)
        return {"ok": True, "installed": info["latest"], "changed": changed, "pip": pip_out, "restart_required": True}

    def close(self) -> None:
        self._client.close()


def _sync_tree(src: Path, dst: Path) -> int:
    changed = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        parts = rel.parts
        if not parts or parts[0] in PRESERVE_DIRS or (len(parts) == 1 and parts[0] in PRESERVE_FILES):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        data = path.read_bytes()
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            changed += 1
    return changed


def restart_app() -> None:
    """Replace this process with a fresh one (same interpreter and arguments)."""
    log.info("restarting: %s %s", sys.executable, sys.argv)
    if os.name == "nt":
        subprocess.Popen([sys.executable, *sys.argv], cwd=os.getcwd(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        os._exit(0)
    os.execv(sys.executable, [sys.executable, *sys.argv])
