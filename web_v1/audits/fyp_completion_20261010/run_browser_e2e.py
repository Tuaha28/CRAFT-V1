"""Run Edge E2E against disposable application DB and local-only services.

No legacy database alias is configured. The TEST_SIM and TEST_PRIORITY scopes,
manager, schedules, and migrations live only in a TemporaryDirectory.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BACKEND = ROOT / "web_v1/backend"
FRONTEND = ROOT / "web_v1/frontend"
BACKEND_PYTHON = BACKEND / ".venv/Scripts/python.exe"
WORKER_PYTHON = ROOT / "venv/Scripts/python.exe"


def unused(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def ready(url: str, process: subprocess.Popen, timeout: float = 60) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError(f"SERVICE_EXITED:{url}:{process.returncode}")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"SERVICE_NOT_READY:{url}")


def stop_launched(process: subprocess.Popen) -> None:
    if process.poll() is None:
        # Exact PID created by this runner, including its own dev-server child.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, text=True, check=False)


def main() -> int:
    if not all(unused(port) for port in (8000, 8001, 5173)):
        raise RuntimeError("BROWSER_TEST_PORT_IN_USE: refusing to attach to existing services")
    if not BACKEND_PYTHON.is_file() or not WORKER_PYTHON.is_file():
        raise RuntimeError("REQUIRED_VIRTUALENV_MISSING")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    browser_log = HERE / f"browser_e2e_{stamp}.json"
    processes: list[subprocess.Popen] = []
    logs = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="craft-browser-test-") as directory:
        temp = Path(directory)
        env = {**os.environ,
               "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
               "DJANGO_SETTINGS_MODULE": "config.settings_integration",
               "CRAFT_WEB_DB_PATH": str(temp / "craft_web_test.sqlite3"),
               "CRAFT_TEST_BROWSER_ISOLATED": "1",
               "CRAFT_BROWSER_ARTIFACT_DIR": str(HERE / f"browser_artifacts_{stamp}"),
               "CRAFT_BROWSER_TEST_USER": "browser_manager",
               "CRAFT_BROWSER_TEST_PASSWORD": secrets.token_urlsafe(24),
               "CRAFT_BROWSER_TEST_WORKER_PYTHON": str(BACKEND_PYTHON),
               "CRAFT_WORKER_PYTHON": str(WORKER_PYTHON)}
        sys.path.insert(0, str(BACKEND))
        os.environ.update({key: env[key] for key in ("DJANGO_SECRET_KEY", "DJANGO_SETTINGS_MODULE", "CRAFT_WEB_DB_PATH")})
        import django
        django.setup()
        from django.core.management import call_command
        from django.contrib.auth import get_user_model
        call_command("migrate", interactive=False, verbosity=0)
        get_user_model().objects.create_superuser(env["CRAFT_BROWSER_TEST_USER"],
                                                   "browser-test@example.invalid", env["CRAFT_BROWSER_TEST_PASSWORD"])
        call_command("init_craft_sim_scope", verbosity=0)
        call_command("init_craft_priority_scope", verbosity=0)
        commands = [
            ([str(BACKEND_PYTHON), str(BACKEND / "manage.py"), "runserver", "127.0.0.1:8000", "--noreload", "--settings=config.settings"],
             ROOT, "demo", "http://127.0.0.1:8000/api/v1/health"),
            ([str(BACKEND_PYTHON), str(BACKEND / "manage.py"), "runserver", "127.0.0.1:8001", "--noreload", "--settings=config.settings_integration"],
             ROOT, "governance", "http://127.0.0.1:8001/api/v1/governance/session"),
            (["npm.cmd", "run", "dev", "--", "--host", "127.0.0.1"], FRONTEND, "vite", "http://127.0.0.1:5173"),
        ]
        result = {"classification": "ISOLATED_TEST_SIM_AND_PRIORITY_BROWSER_NOT_LIVE", "status": "NOT_RUN",
                  "database": "DISPOSABLE_APPLICATION_SQLITE", "legacy_db_writes": 0,
                  "live_activation": False, "services": [], "elapsed_seconds": None}
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            for command, cwd, name, url in commands:
                handle = (temp / f"{name}.log").open("w", encoding="utf-8")
                logs.append(handle)
                process = subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                           creationflags=flags)
                processes.append(process)
                ready(url, process)
                result["services"].append(name)
            command = [str(FRONTEND / "node_modules/.bin/playwright.cmd"), "test", "e2e/governance.spec.ts",
                       "--config=playwright.config.ts"]
            test = subprocess.run(command, cwd=FRONTEND, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=240)
            result["status"] = "PASS" if test.returncode == 0 else "FAIL"
            result["playwright_returncode"] = test.returncode
            result["playwright_output"] = test.stdout + "\n" + test.stderr
        except Exception as exc:
            result["status"] = "BLOCKED"
            result["error"] = f"{type(exc).__name__}:{exc}"
        finally:
            result["elapsed_seconds"] = time.perf_counter() - started
            for process in reversed(processes):
                stop_launched(process)
            from django.db import connections
            connections.close_all()
            for handle in logs:
                handle.close()
            result["service_logs"] = {name: (temp / f"{name}.log").read_text(encoding="utf-8", errors="replace")[-10000:]
                                      for name in result["services"]}
            browser_log.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({"status": result["status"], "seconds": result["elapsed_seconds"],
                              "evidence": str(browser_log), "error": result.get("error")}, default=str))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
