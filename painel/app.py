# -*- coding: utf-8 -*-
"""Painel web local para o Twitch Channel Points Miner.

Uso:
    pythonw painel/app.py              abre o painel no navegador
    pythonw painel/app.py --autostart  modo inicialização do Windows (sem abrir o navegador)
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

# O Python portátil do instalador não coloca a pasta do script no sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from painel_config import (  # noqa: E402
    CHAT_MODES,
    CONDITIONS,
    DATA_DIR,
    DELAY_MODES,
    FILTER_KEYS,
    PAINEL_DIR,
    PRIORITIES,
    ROOT,
    STATUS_FILE,
    STRATEGIES,
    ConfigError,
    cookies_file,
    load_config,
    save_config,
)
from updater import UpdateError, Updater, install_requirements  # noqa: E402

HOST = "127.0.0.1"
PORT = 8090
URL = f"http://{HOST}:{PORT}"
AUTOSTART = "--autostart" in sys.argv
RESTARTING = "--reiniciando" in sys.argv
START_MINER = "--iniciar-miner" in sys.argv
BOOT_ID = time.time()

# pythonw não tem console: manda a saída para um arquivo para facilitar diagnóstico
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA_DIR / "painel.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log


def panel_already_running():
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


if RESTARTING:
    # Espera o painel antigo liberar a porta depois de uma atualização
    for _ in range(60):
        if not panel_already_running():
            break
        time.sleep(0.5)

if panel_already_running():
    if not AUTOSTART:
        webbrowser.open(URL)
    sys.exit(0)

from flask import Flask, abort, jsonify, request, send_from_directory  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOGIN_CODE_RE = re.compile(r"enter this code:\s*([A-Z0-9]+)")
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
STARTUP_FILE = STARTUP_DIR / "Twitch Miner Painel.vbs"
STOP_TIMEOUT = 45
RESTART_DELAY = 30
UPDATE_FIRST_CHECK = 15
UPDATE_CHECK_INTERVAL = 6 * 3600


def venv_exe(name):
    exe = Path(sys.executable).with_name(name)
    return str(exe if exe.is_file() else sys.executable)


class Miner:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.logs = deque(maxlen=3000)
        self.seq = 0
        self.started_at = None
        self.stop_requested = False
        self.login_code = None
        self.last_exit = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def log(self, line):
        with self.lock:
            self.seq += 1
            self.logs.append({"id": self.seq, "text": line})
        match = LOGIN_CODE_RE.search(line)
        if match:
            self.login_code = match.group(1)
        elif "Saving cookies" in line or "Loading data for" in line:
            self.login_code = None

    def logs_since(self, since):
        with self.lock:
            return [entry for entry in self.logs if entry["id"] > since]

    def start(self):
        with self.lock:
            if self.running:
                return
            cfg = load_config()
            if not cfg["username"]:
                raise ConfigError("Informe seu usuário da Twitch em Configurações antes de iniciar.")
            if not cfg["followers"] and not any(s["enabled"] for s in cfg["streamers"]):
                raise ConfigError("Adicione pelo menos um canal (ou ative 'Usar canais que eu sigo').")
            try:
                STATUS_FILE.unlink()
            except FileNotFoundError:
                pass
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            self.proc = subprocess.Popen(
                [venv_exe("python.exe"), "-u", str(PAINEL_DIR / "runner.py")],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started_at = time.time()
            self.stop_requested = False
            self.login_code = None
            self.last_exit = None
            proc = self.proc
        self.log("[painel] Miner iniciado.")
        threading.Thread(target=self._read_output, args=(proc,), daemon=True).start()

    def _read_output(self, proc):
        for raw in proc.stdout:
            line = ANSI_RE.sub("", raw).rstrip()
            if line.strip():
                self.log(line)
        code = proc.wait()
        self.last_exit = code
        self.login_code = None
        self.log(f"[painel] Miner encerrado (código {code}).")
        if code != 0 and not self.stop_requested and load_config()["auto_restart"]:
            self.log(f"[painel] O miner parou inesperadamente. Reiniciando em {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)
            if not self.stop_requested and not self.running:
                try:
                    self.start()
                except ConfigError as exc:
                    self.log(f"[painel] {exc}")

    def stop(self, wait=False):
        proc = self.proc
        self.stop_requested = True
        if proc is None or proc.poll() is not None:
            return
        self.log("[painel] Encerrando o miner, aguarde...")
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        except OSError:
            pass
        killer = threading.Thread(target=self._force_kill, args=(proc,), daemon=True)
        killer.start()
        if wait:
            killer.join()

    def _force_kill(self, proc):
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.log("[painel] O miner não respondeu e foi finalizado à força.")

    def restart(self):
        self.stop(wait=True)
        try:
            self.start()
        except ConfigError as exc:
            self.log(f"[painel] {exc}")


miner = Miner()
updater = Updater()
update_lock = threading.Lock()


def run_update():
    """Para o miner, instala a atualização e volta a minerar se ele estava rodando."""
    if not update_lock.acquire(blocking=False):
        return
    updater.state["applying"] = True
    was_running = miner.running
    try:
        miner.log("[painel] Instalando atualização do miner...")
        if was_running:
            miner.stop(wait=True)
        try:
            changed = updater.apply()
            if {"requirements.txt", "painel/requirements.txt"} & set(changed):
                miner.log("[painel] Atualizando dependências (pode levar alguns minutos)...")
                install_requirements(venv_exe("python.exe"))
            miner.log(f"[painel] Atualização instalada (miner versão {updater.state['current']}).")
            if any(path.startswith("painel/") for path in changed):
                restart_panel(start_miner=was_running)
        except UpdateError as exc:
            updater.state["error"] = str(exc)
            miner.log(f"[painel] Falha ao atualizar: {exc}")
        if was_running:
            try:
                miner.start()
            except ConfigError as exc:
                miner.log(f"[painel] {exc}")
    finally:
        updater.state["applying"] = False
        update_lock.release()


def restart_panel(start_miner):
    """Abre uma nova instância do painel (com o código novo) e encerra esta."""
    miner.log("[painel] O painel também foi atualizado. Reiniciando o painel...")
    args = [venv_exe("pythonw.exe"), str(PAINEL_DIR / "app.py"), "--autostart", "--reiniciando"]
    if start_miner:
        args.append("--iniciar-miner")
    subprocess.Popen(args, cwd=PAINEL_DIR, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    time.sleep(1)  # dá tempo de a página receber o último status
    exit_panel()


tray = None


def exit_panel():
    """Encerra o processo do painel, tirando antes o ícone da bandeja."""
    if tray is not None:
        tray.stop()
    os._exit(0)


def quit_all():
    miner.stop(wait=True)
    exit_panel()


def run_tray():
    """Ícone perto do relógio: um clique abre o painel; o botão direito mostra o menu."""
    global tray
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("pystray não instalado: ícone na bandeja desativado")
        return

    def refresh_title():
        while True:
            tray.title = "Twitch Miner - " + ("minerando" if miner.running else "parado")
            time.sleep(5)

    menu = pystray.Menu(
        pystray.MenuItem("Abrir painel", lambda: webbrowser.open(URL), default=True),
        pystray.Menu.SEPARATOR,
        # Em outra thread para não travar o menu enquanto o miner encerra
        pystray.MenuItem("Encerrar Twitch Miner", lambda: threading.Thread(target=quit_all).start()),
    )
    tray = pystray.Icon("twitch-miner", Image.open(PAINEL_DIR / "static" / "icone.ico"), "Twitch Miner", menu)
    threading.Thread(target=refresh_title, daemon=True).start()
    tray.run()


def update_loop():
    time.sleep(UPDATE_FIRST_CHECK)
    while True:
        state = updater.check()
        if state["available"] and load_config()["auto_update"]:
            miner.log(f"[painel] Nova versão do miner encontrada ({state['latest']}). Atualizando automaticamente...")
            run_update()
        time.sleep(UPDATE_CHECK_INTERVAL)


app = Flask(__name__, static_folder=None)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.before_request
def protect_api():
    # Um cabeçalho personalizado impede que outros sites façam POST no painel (CORS)
    if request.method == "POST" and request.headers.get("X-Painel") != "1":
        abort(403)


def error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


@app.get("/")
def index():
    return send_from_directory(PAINEL_DIR / "static", "index.html")


@app.get("/api/options")
def options():
    return jsonify(
        strategies=STRATEGIES,
        priorities=PRIORITIES,
        chat_modes=CHAT_MODES,
        delay_modes=DELAY_MODES,
        filter_keys=FILTER_KEYS,
        conditions=CONDITIONS,
    )


@app.get("/api/config")
def get_config():
    return jsonify(load_config())


@app.post("/api/config")
def post_config():
    try:
        cfg = save_config(request.get_json(force=True))
    except ConfigError as exc:
        return error(str(exc))
    return jsonify(ok=True, config=cfg, running=miner.running)


@app.get("/api/status")
def status():
    cfg = load_config()
    channels = None
    if miner.running and STATUS_FILE.is_file():
        try:
            channels = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            channels = None
    return jsonify(
        running=miner.running,
        started_at=miner.started_at if miner.running else None,
        login_code=miner.login_code if miner.running else None,
        logged_in=bool(cfg["username"]) and cookies_file(cfg["username"]).is_file(),
        last_exit=miner.last_exit,
        channels=channels,
        analytics_url=f"http://127.0.0.1:{cfg['analytics_port']}" if cfg["enable_analytics"] else None,
        autostart=STARTUP_FILE.is_file(),
        update=updater.snapshot(),
        boot_id=BOOT_ID,
    )


@app.get("/api/logs")
def logs():
    return jsonify(miner.logs_since(request.args.get("since", 0, type=int)))


@app.post("/api/start")
def start():
    try:
        miner.start()
    except ConfigError as exc:
        return error(str(exc))
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    miner.stop()
    return jsonify(ok=True)


@app.post("/api/restart")
def restart():
    threading.Thread(target=miner.restart, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    if miner.running:
        return error("Pare o miner antes de trocar de conta.")
    username = load_config()["username"]
    if username:
        try:
            cookies_file(username).unlink()
        except FileNotFoundError:
            pass
    return jsonify(ok=True)


@app.post("/api/autostart")
def autostart():
    enabled = bool((request.get_json(force=True) or {}).get("enabled"))
    if enabled:
        pythonw = venv_exe("pythonw.exe")
        script = PAINEL_DIR / "app.py"
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        STARTUP_FILE.write_text(
            'Set shell = CreateObject("WScript.Shell")\n'
            f'shell.CurrentDirectory = "{PAINEL_DIR}"\n'
            f'shell.Run """{pythonw}"" ""{script}"" --autostart", 0, False\n',
            encoding="utf-16",
        )
    else:
        try:
            STARTUP_FILE.unlink()
        except FileNotFoundError:
            pass
    return jsonify(ok=True, autostart=STARTUP_FILE.is_file())


@app.post("/api/update/check")
def update_check():
    if updater.state["applying"]:
        return error("Uma atualização está sendo instalada.")
    state = updater.check()
    if state["error"]:
        return error(state["error"])
    return jsonify(ok=True, update=state)


@app.post("/api/update/apply")
def update_apply():
    if updater.state["applying"]:
        return error("A atualização já está em andamento.")
    threading.Thread(target=run_update, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/shutdown")
def shutdown():
    def finish():
        time.sleep(0.5)  # deixa a resposta chegar ao navegador
        quit_all()

    threading.Thread(target=finish, daemon=True).start()
    return jsonify(ok=True)


def main():
    cfg = load_config()
    if (cfg["start_miner_with_panel"] or START_MINER) and cfg["username"]:
        try:
            miner.start()
        except ConfigError as exc:
            miner.log(f"[painel] {exc}")
    threading.Thread(target=update_loop, name="Atualizações", daemon=True).start()
    threading.Thread(target=run_tray, name="Bandeja", daemon=True).start()
    if not AUTOSTART:
        threading.Timer(1.0, webbrowser.open, args=(URL,)).start()
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
