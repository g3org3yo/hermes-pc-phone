"""Mesh Button — backend routes.

Mounted by the Hermes gateway at ``/api/plugins/mesh-button/`` and reached from
the desktop half (``desktop/plugin.js``) through ``ctx.rest()``.

WHY A BACKEND
-------------
The desktop UI is sandboxed in the renderer and cannot spawn processes. The
broker (``python -m handoff.mesh_broker`` from the Android PC-companion repo) is
a long-lived process that must outlive a page view, so it is started here, in
the gateway process, which does have OS access.

WHAT THE BROKER IS
------------------
``HenWorks/Hermes-agent-android-PC-companion-app``: the PC half of the Hermes
Android app. It listens on a fixed LAN port (51379 by default) and lets the
paired phone dispatch tasks to this PC's ``hermes`` and hand conversations over.
It prints a pairing QR + a local web console URL at every start, and pairing is
pair-once (``peers.json`` survives restarts).

DETECTION IS PORT-BASED, NOT NAME-BASED
--------------------------------------
The broker is reachable at ``<lan-ip>:51379``, so "is it up" is answered by a
LISTENING socket on that port — which also finds instances started by the tray
or by a scheduled task, not just the one we spawned.

Routes
------
GET  /status             -> {running, pid, port, host, console_url, paired, peers,
                             tasks, results, repo, python, last_error}
GET  /log?tail=40        -> last log lines (broker.log)
POST /start              -> start the broker hidden (stdout -> broker.log)
POST /stop               -> terminate it and its task children
POST /restart            -> stop + start (fresh 5-minute pairing window)
POST /pair               -> restart + open the console (QR) in the browser
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter()

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
DEFAULT_PORT = 51379
CREATE_NEW_CONSOLE = 0x00000010

_lock = threading.RLock()
_child: Optional[subprocess.Popen] = None
_last_error: Optional[str] = None


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def _home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


def _mesh_dir() -> Path:
    return _home() / "mesh"


def _log_path() -> Path:
    return _mesh_dir() / "broker.log"


def _peers_path() -> Path:
    return _mesh_dir() / "peers.json"


def _queue_path() -> Path:
    return _mesh_dir() / "queue.db"


def _port() -> int:
    try:
        return int(os.environ.get("MESH_PORT") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def _repo() -> Optional[Path]:
    """The companion repo (must be the cwd: the broker imports top-level modules)."""
    cands: List[Path] = []
    env = os.environ.get("MESH_REPO")
    if env:
        cands.append(Path(env))
    cands += [
        # our own first-run checkout (created by /setup/install)
        _mesh_dir() / "companion",
        # a clone the user made themselves, wherever they like
        Path("C:/Projects/hermes-companion") if IS_WINDOWS else Path.home() / "hermes-companion",
        Path.home() / "hermes-companion",
        Path.home() / "Downloads" / "hermes-companion",
        Path.home() / "Downloads" / "Hermes-agent-android-PC-companion-app",
        # the same repo installed as an agent plugin (hermes plugins install HenWorks/...)
        _home() / "plugins" / "hermes-companion",
        _home() / "plugins" / "Hermes-agent-android-PC-companion-app",
    ]
    for cand in cands:
        try:
            if (cand / "handoff" / "mesh_broker.py").exists():
                return cand
        except Exception:
            continue
    return None


def _python() -> Optional[str]:
    """Broker interpreter: the plugin's dedicated venv, never the agent's own."""
    env = os.environ.get("MESH_PYTHON")
    if env and Path(env).exists():
        return env
    for venv in (Path(_mesh_dir()) / "venv", (_repo() or Path()) / ".venv", (_repo() or Path()) / "venv"):
        win = venv / "Scripts" / "python.exe"
        posix = venv / "bin" / "python3"
        if IS_WINDOWS and win.exists():
            return str(win)
        if posix.exists():
            return str(posix)
    cand = _mesh_dir() / "venv" / "Scripts" / ("python.exe" if IS_WINDOWS else "python3")
    if IS_WINDOWS and cand.exists():
        return str(cand)
    posix = _mesh_dir() / "venv" / "bin" / "python3"
    if posix.exists():
        return str(posix)
    if cand.exists():
        return str(cand)
    return None


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def _listening_pid() -> Optional[int]:
    """PID owning a LISTENING socket on the mesh port (any starter)."""
    port = _port()
    psutil = _psutil()
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return conn.pid
        except Exception:
            pass
    # fallback: parse netstat (no psutil permissions needed)
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15,
            errors="replace", creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if f":{port}" in line and "LISTEN" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def _lan_ip() -> Optional[str]:
    """The address the phone connects to (the broker binds the LAN interface)."""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def _console_url() -> Optional[str]:
    """Latest local console URL printed by the broker (random port per run)."""
    hits = _console_hits()
    return hits[-1] if hits else None


def _console_hits() -> list:
    try:
        text = _log_path().read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return re.findall(r"Console: (http://127\.0\.0\.1:\d+/)/?", text)


def _console_alive(url: Optional[str]) -> bool:
    if not url:
        return False
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - localhost only
            return 200 <= resp.status < 400
    except Exception:
        return False


def _peers() -> Dict[str, Any]:
    try:
        data = json.loads(_peers_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"count": 0, "ids": []}
        return {"count": len(data), "ids": [str(k)[:12] for k in data]}
    except Exception:
        return {"count": 0, "ids": []}


def _queue_counts() -> Dict[str, int]:
    """tasks/results in the broker's SQLite outbox (read-only, never creates it)."""
    out = {"tasks": 0, "results": 0}
    path = _queue_path()
    if not path.exists() or path.stat().st_size == 0:
        return out
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            for table in ("tasks", "results"):
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 - fixed names
                    out[table] = int(row[0]) if row else 0
                except Exception:
                    out[table] = 0
        finally:
            conn.close()
    except Exception:
        pass
    return out


def _status_obj() -> Dict[str, Any]:
    pid = _listening_pid()
    peers = _peers()
    counts = _queue_counts()
    url = _console_url()
    alive = _console_alive(url)
    return {
        "running": pid is not None,
        "pid": pid,
        "port": _port(),
        "host": _lan_ip(),
        "console_url": url if alive else None,
        "console_alive": alive,
        "paired": peers["count"],
        "peers": peers["ids"],
        "tasks": counts["tasks"],
        "results": counts["results"],
        "repo": str(_repo() or ""),
        "python": str(_python() or ""),
        "home": str(_home()),
        "log": str(_log_path()),
        "platform": sys.platform,
        "last_error": _last_error,
    }


# --------------------------------------------------------------------------- #
# process control
# --------------------------------------------------------------------------- #
def _kill_tree(pid: int) -> bool:
    psutil = _psutil()
    if psutil is None:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
            return True
        except Exception as exc:
            globals()["_last_error"] = f"Could not stop pid {pid}: {exc}"
            return False
    try:
        root = psutil.Process(pid)
    except Exception:
        return False
    try:
        victims = root.children(recursive=True)
    except Exception:
        victims = []
    for proc in victims + [root]:
        try:
            proc.terminate()
        except Exception:
            pass
    _gone, alive = psutil.wait_procs(victims + [root], timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    return True


def _start() -> Dict[str, Any]:
    """Start the broker hidden; returns once the port is listening (max ~10 s)."""
    global _child, _last_error
    with _lock:
        pid = _listening_pid()
        if pid is not None:
            return {"ok": True, "already_running": True, "pid": pid}
        repo = _repo()
        py = _python()
        if repo is None:
            _last_error = (
                "The PC companion is not installed yet — open the PC - Phone page and run Setup "
                "(or clone https://github.com/HenWorks/Hermes-agent-android-PC-companion-app yourself "
                "and point MESH_REPO at it)."
            )
            return {"ok": False, "error": _last_error}
        if py is None:
            _last_error = f"Its Python venv is missing under {_mesh_dir() / 'venv'} — run Setup on the PC - Phone page."
            return {"ok": False, "error": _last_error}
        _mesh_dir().mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(_home())
        env["BROWSER"] = "none"
        env.pop("PYTHONHOME", None)
        kwargs: Dict[str, Any] = {"cwd": str(repo), "env": env, "stdin": subprocess.DEVNULL}
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        log = _log_path().open("ab")
        try:
            _child = subprocess.Popen(  # noqa: S603
                [py, "-u", "-m", "handoff.mesh_broker"],
                stdout=log, stderr=subprocess.STDOUT, **kwargs,
            )
        except Exception as exc:
            _last_error = f"Failed to start the broker: {exc}"
            return {"ok": False, "error": _last_error}
        finally:
            log.close()
        _last_error = None
    hits_before = len(_console_hits())
    for _ in range(40):
        time.sleep(0.25)
        pid = _listening_pid()
        if pid is not None:
            break
    else:
        _last_error = "The broker did not open its port within 10 s — see broker.log."
        return {"ok": False, "error": _last_error, "pid": _child.pid if _child else None}
    # The console port is random per run: wait for the new "Console:" line so the
    # UI never links to the previous run's dead port.
    url = None
    for _ in range(32):
        hits = _console_hits()
        if len(hits) > hits_before:
            url = hits[-1]
            break
        time.sleep(0.25)
    if url is None:
        url = _console_url()
    return {"ok": True, "pid": pid, "console_url": url, "console_alive": _console_alive(url)}


def _stop() -> Dict[str, Any]:
    global _child
    with _lock:
        pid = _listening_pid() or (_child.pid if _child and _child.poll() is None else None)
        if pid is None:
            _child = None
            return {"ok": True, "stopped": []}
        _kill_tree(pid)
        _child = None
    for _ in range(20):
        time.sleep(0.2)
        if _listening_pid() is None:
            return {"ok": True, "stopped": [pid]}
    return {"ok": False, "error": f"pid {pid} is still listening on {_port()}.", "stopped": []}


def _restart() -> Dict[str, Any]:
    stop = _stop()
    if not stop.get("ok"):
        return stop
    time.sleep(0.5)
    return _start()



# --------------------------------------------------------------------------- #
# first-run setup: clone the companion + build its isolated venv
# --------------------------------------------------------------------------- #
COMPANION_REPO = "https://github.com/HenWorks/Hermes-agent-android-PC-companion-app"
REQUIRED_MODULES = "import nacl, zeroconf, qrcode"
_SETUP_LOCK = threading.Lock()
_setup: Dict[str, Any] = {"running": False, "step": "", "done": False, "ok": None, "error": None}
_deps_cache: Dict[str, Any] = {"python": None, "ok": None, "at": 0.0}


def _setup_log_path() -> Path:
    return _mesh_dir() / "setup.log"


def _log_setup(line: str) -> None:
    try:
        _mesh_dir().mkdir(parents=True, exist_ok=True)
        with _setup_log_path().open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


def _run(args: List[str], cwd: Optional[str] = None) -> None:
    """Run a command, streaming its output into setup.log, raising on failure."""
    _log_setup("$ " + " ".join(args))
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    proc = subprocess.Popen(  # noqa: S603
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1, creationflags=flags,
    )
    assert proc.stdout is not None
    tail: List[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _log_setup(line)
            tail = (tail + [line])[-12:]
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"`{args[0]}` exited with {code}: " + " / ".join(tail[-3:]))


def _deps_ok(py: Optional[str], *, fresh: bool = False) -> Optional[bool]:
    """True when the broker venv can import its deps (cached for a minute)."""
    if not py:
        return False
    now = time.time()
    if not fresh and _deps_cache["python"] == py and _deps_cache["ok"] is not None and now - _deps_cache["at"] < 60:
        return bool(_deps_cache["ok"])
    ok: Optional[bool]
    try:
        flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
        proc = subprocess.run(  # noqa: S603
            [py, "-c", REQUIRED_MODULES], capture_output=True, text=True, timeout=60, creationflags=flags,
        )
        ok = proc.returncode == 0
    except Exception:
        ok = None
    _deps_cache.update(python=py, ok=ok, at=now)
    return ok


def _setup_obj() -> Dict[str, Any]:
    repo, py = _repo(), _python()
    return {
        "ready": bool(repo and py and _deps_ok(py)),
        "repo": str(repo) if repo else None,
        "python": py,
        "deps_ok": _deps_ok(py) if py else False,
        "companion_repo": COMPANION_REPO,
        "home": str(_home()),
        "running": _setup["running"],
        "step": _setup["step"],
        "done": _setup["done"],
        "ok": _setup["ok"],
        "error": _setup["error"],
    }


def _bootstrap_run() -> None:
    try:
        repo = _repo()
        if repo is None:
            _setup["step"] = "cloning the PC companion"
            target = Path(os.environ.get("MESH_REPO") or (_mesh_dir() / "companion"))
            target.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "clone", "--depth", "1", COMPANION_REPO, str(target)], cwd=str(target.parent))
            if not (target / "handoff" / "mesh_broker.py").exists():
                raise RuntimeError("clone finished but handoff/mesh_broker.py is missing — is git installed?")
            repo = target
        py = _python()
        if py is None:
            _setup["step"] = "creating the isolated venv"
            venv_dir = _mesh_dir() / "venv"
            venv_dir.parent.mkdir(parents=True, exist_ok=True)
            _run([sys.executable, "-m", "venv", str(venv_dir)])
            py = _python()
            if py is None:
                raise RuntimeError("the venv was created but no interpreter was found inside it")
        _setup["step"] = "installing Python packages"
        req = repo / "handoff" / "requirements.txt"
        args = [py, "-m", "pip", "install", "--disable-pip-version-check"]
        args += ["-r", str(req)] if req.exists() else ["PyNaCl", "zeroconf", "qrcode", "pillow"]
        _run(args)
        _setup["step"] = "verifying"
        if not _deps_ok(py, fresh=True):
            raise RuntimeError("the packages still do not import — see the log above")
        _setup.update(running=False, done=True, ok=True, step="ready")
    except Exception as exc:  # noqa: BLE001
        _log_setup(f"ERROR: {exc}")
        _setup.update(running=False, done=True, ok=False, step="failed", error=str(exc))


def _bootstrap() -> Dict[str, Any]:
    with _SETUP_LOCK:
        if _setup["running"]:
            return {"ok": True, "started": False, "already_running": True}
        _setup.update(running=True, done=False, ok=None, error=None, step="starting")
    _log_setup("--- setup started ---")
    threading.Thread(target=_bootstrap_run, daemon=True).start()
    return {"ok": True, "started": True}


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.get("/status")
def status() -> Dict[str, Any]:
    return _status_obj()


@router.get("/log")
def log(tail: int = 40) -> Dict[str, Any]:
    try:
        lines = _log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "lines": []}
    n = max(1, min(int(tail or 40), 400))
    return {"ok": True, "lines": lines[-n:]}


@router.post("/start")
def start() -> Dict[str, Any]:
    return _start()


@router.post("/stop")
def stop() -> Dict[str, Any]:
    return _stop()


@router.post("/restart")
def restart() -> Dict[str, Any]:
    return _restart()


@router.post("/pair")
def pair() -> Dict[str, Any]:
    """Fresh 5-minute pairing window + open the console (QR) in the browser."""
    before = len(re.findall(r"Console: http://127\.0\.0\.1:\d+/",
                            _log_path().read_text(encoding="utf-8", errors="replace")
                            if _log_path().exists() else ""))
    res = _restart()
    if not res.get("ok"):
        return res
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.4)
        url = _console_url()
        hits = len(re.findall(r"Console: http://127\.0\.0\.1:\d+/",
                              _log_path().read_text(encoding="utf-8", errors="replace")))
        if url and hits > before:
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return {"ok": True, "console_url": url, "opened": True}
    url = _console_url()
    if url:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return {"ok": bool(url), "console_url": url, "opened": bool(url)}


@router.get("/setup")
def setup_status() -> Dict[str, Any]:
    return _setup_obj()


@router.post("/setup/install")
def setup_install() -> Dict[str, Any]:
    return _bootstrap()


@router.get("/setup/log")
def setup_log(tail: int = 40) -> Dict[str, Any]:
    try:
        lines = _setup_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {"ok": True, "lines": []}
    n = max(1, min(int(tail or 40), 400))
    return {"ok": True, "lines": lines[-n:]}
