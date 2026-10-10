# -*- coding: utf-8 -*-
"""Verifica e instala atualizações do miner e do painel publicadas no GitHub.

Usa o git quando a pasta é um clone; caso contrário compara os arquivos com o
GitHub e baixa o .zip. Os dados do usuário (configurações do painel, cookies,
logs e analytics) nunca são tocados.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import PurePosixPath

import requests

from painel_config import ROOT

# O fork recebe automaticamente as correções do repositório original (rdavydov)
REPO = "alexsandrejr/Twitch-Channel-Points-Miner-v2"
BRANCH = "master"
REMOTE_REF = f"origin/{BRANCH}"
VERSION_FILE = "TwitchChannelPointsMiner/__init__.py"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{VERSION_FILE}"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
# Caminhos que pertencem ao usuário e nunca são sobrescritos no modo .zip
PROTECTED = ("painel/data/", "cookies/", "logs/", "analytics/", ".git/", ".venv/", "run.py")
# Dependências só de desenvolvimento, que o miner não precisa para rodar
DEV_REQUIREMENTS = {"pre-commit"}
VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(RuntimeError):
    pass


def parse_version(text):
    match = VERSION_RE.search(text or "")
    return match.group(1) if match else None


REQUIREMENTS_FILES = ("requirements.txt", "painel/requirements.txt")


def read_requirements():
    lines = []
    for name in REQUIREMENTS_FILES:
        try:
            lines += (ROOT / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
    return lines


def is_protected(path):
    return any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED)


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def same_as_github(path, sha):
    """Compara o arquivo local com o do GitHub pelo hash do git (aceita CRLF ou LF)."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return sha in (git_blob_sha(data), git_blob_sha(data.replace(b"\r\n", b"\n")))


def github_get(url, timeout=30):
    headers = {"Accept": "application/vnd.github+json"} if "api.github.com" in url else {}
    try:
        response = requests.get(url, timeout=timeout, headers=headers)
    except requests.RequestException:
        raise UpdateError("Não foi possível falar com o GitHub. Verifique sua internet.")
    if response.status_code in (403, 429):
        raise UpdateError("O GitHub limitou as consultas por agora. Tente de novo em uma hora.")
    if not response.ok:
        raise UpdateError(f"O GitHub respondeu com erro {response.status_code}.")
    return response


def git(*args, timeout=60):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except FileNotFoundError:
        raise UpdateError("Git não encontrado.")
    except subprocess.TimeoutExpired:
        raise UpdateError("O git demorou demais para responder. Verifique sua internet.")
    if result.returncode != 0:
        raise UpdateError((result.stderr or result.stdout).strip() or f"git {args[0]} falhou")
    # Só rstrip: o "git status --porcelain" usa espaços no início da linha
    return result.stdout.rstrip()


def install_requirements(python):
    packages = []
    for line in read_requirements():
        line = line.strip()
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0]
        if line and not line.startswith("#") and name not in DEV_REQUIREMENTS:
            packages.append(line)
    if not packages:
        return
    # Pacotes só com código-fonte (ex.: millify) precisam do setuptools; o Python portátil do
    # instalador ignora o ambiente isolado de montagem do pip, daí o --no-build-isolation
    pip_install(python, "setuptools", "wheel")
    pip_install(python, "--no-build-isolation", *packages)


def pip_install(python, *args):
    result = subprocess.run(
        [python, "-m", "pip", "install", "-q", "--disable-pip-version-check", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError("Falha ao instalar dependências: " + (lines[-1] if lines else "erro desconhecido"))


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "git" if (ROOT / ".git").exists() and shutil.which("git") else "zip"
        self.state = {
            "mode": self.mode,
            "checking": False,
            "applying": False,
            "available": False,
            "current": self.current_version(),
            "latest": None,
            "behind": 0,
            "files": 0,
            "changes": [],
            "last_check": None,
            "error": None,
        }

    @staticmethod
    def current_version():
        try:
            return parse_version((ROOT / VERSION_FILE).read_text(encoding="utf-8"))
        except OSError:
            return None

    def snapshot(self):
        return dict(self.state)

    def check(self):
        if not self.lock.acquire(blocking=False):
            return self.snapshot()
        self.state.update(checking=True, error=None)
        try:
            self.state.update(self._check_git() if self.mode == "git" else self._check_zip())
        except UpdateError as exc:
            self.state["error"] = str(exc)
        finally:
            self.state.update(checking=False, last_check=time.time(), current=self.current_version())
            self.lock.release()
        return self.snapshot()

    def _check_git(self):
        git("fetch", "--quiet", "origin", BRANCH, timeout=120)
        behind = int(git("rev-list", "--count", f"HEAD..{REMOTE_REF}"))
        changes = []
        if behind:
            log = git("log", "--no-merges", "-n", "30", "--date=short", "--format=%ad%x1f%s", f"HEAD..{REMOTE_REF}")
            changes = [dict(zip(("date", "message"), line.split("\x1f", 1))) for line in log.splitlines()]
        latest = parse_version(git("show", f"{REMOTE_REF}:{VERSION_FILE}"))
        return {"available": behind > 0, "behind": behind, "files": 0, "changes": changes, "latest": latest}

    def _check_zip(self):
        tree = github_get(TREE_URL).json().get("tree", [])
        outdated = [
            item["path"]
            for item in tree
            if item.get("type") == "blob"
            and item.get("mode") != "120000"
            and not is_protected(item["path"])
            and not same_as_github(ROOT.joinpath(*item["path"].split("/")), item["sha"])
        ]
        latest = parse_version(github_get(RAW_VERSION_URL).text)
        return {"available": bool(outdated), "behind": 0, "files": len(outdated), "changes": [], "latest": latest}

    def apply(self):
        """Instala a atualização. Retorna a lista de arquivos alterados (caminhos com /)."""
        if not self.lock.acquire(blocking=False):
            raise UpdateError("Já existe uma verificação ou atualização em andamento.")
        self.state["error"] = None
        try:
            changed = self._apply_git() if self.mode == "git" else self._apply_zip()
            self.state.update(available=False, behind=0, files=0, changes=[], current=self.current_version())
            return changed
        except UpdateError as exc:
            self.state["error"] = str(exc)
            raise
        finally:
            self.lock.release()

    def _apply_git(self):
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != BRANCH:
            raise UpdateError(f"A pasta está no branch '{branch}'. Volte para '{BRANCH}' para atualizar.")
        modified = git("status", "--porcelain", "--untracked-files=no")
        if modified:
            files = ", ".join(line[3:] for line in modified.splitlines()[:5])
            raise UpdateError(
                f"Há arquivos modificados nesta pasta ({files}). "
                "Desfaça as alterações ou atualize manualmente com o git."
            )
        git("fetch", "--quiet", "origin", BRANCH, timeout=120)
        before = git("rev-parse", "HEAD")
        try:
            git("merge", "--ff-only", REMOTE_REF)
        except UpdateError:
            self._merge()
        return git("diff", "--name-only", before, "HEAD").splitlines()

    def _merge(self):
        # Há commits locais que não estão no GitHub: junta com um merge normal
        identity = []
        try:
            git("config", "user.email")
        except UpdateError:
            identity = ["-c", "user.name=Twitch Miner Painel", "-c", "user.email=painel@localhost"]
        try:
            git(*identity, "merge", "--no-edit", REMOTE_REF)
        except UpdateError:
            try:
                git("merge", "--abort")
            except UpdateError:
                pass
            raise UpdateError(
                "A atualização entra em conflito com alterações locais. Atualize manualmente com o git."
            )

    def _apply_zip(self):
        response = github_get(ZIP_URL, timeout=180)
        try:
            archive = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile:
            raise UpdateError("O arquivo baixado do GitHub está corrompido. Tente de novo.")
        changed = []
        with archive:
            for info in archive.infolist():
                # Remove a pasta raiz do .zip (Twitch-Channel-Points-Miner-v2-master/)
                parts = PurePosixPath(info.filename).parts[1:]
                path = "/".join(parts)
                if info.is_dir() or not parts or ".." in parts or is_protected(path):
                    continue
                data = archive.read(info)
                target = ROOT.joinpath(*parts)
                try:
                    if target.read_bytes() == data:
                        continue
                except OSError:
                    pass
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                changed.append(path)
        return changed
