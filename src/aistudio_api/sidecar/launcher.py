"""Supervise aistudio-api, uncut-bridge, and the public path router.

Forwards SIGTERM/SIGINT directly to aistudio-api and waits for its graceful
shutdown (including the final GitHub state push) before exiting.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parents[1]
REPO_ROOT = SRC_DIR.parent


def _terminate_wait(proc: subprocess.Popen | None, name: str, timeout: float) -> int | None:
    if proc is None:
        return None
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[launcher] {name} did not exit within {timeout}s; killing", flush=True)
            try:
                proc.kill()
            except OSError:
                pass
            return proc.wait()
    return proc.returncode


def main() -> int:
    public_port = int(os.environ.get("PUBLIC_PORT") or os.environ.get("PORT") or "8080")
    internal_port = int(
        os.environ.get("AISTUDIO_PORT") or ("18080" if public_port == 8080 else "8080")
    )
    bridge_port = int(os.environ.get("OAI_BRIDGE_PORT") or "8765")

    base_env = os.environ.copy()
    py_path = base_env.get("PYTHONPATH", "")
    if str(SRC_DIR) not in py_path.split(os.pathsep):
        base_env["PYTHONPATH"] = (
            f"{SRC_DIR}{os.pathsep}{py_path}" if py_path else str(SRC_DIR)
        )

    api_env = dict(base_env, AISTUDIO_PORT=str(internal_port))
    bridge_env = dict(
        base_env,
        OAI_BRIDGE_HOST=base_env.get("OAI_BRIDGE_HOST", "127.0.0.1"),
        OAI_BRIDGE_PORT=str(bridge_port),
        OAI_BRIDGE_UPSTREAM=f"http://127.0.0.1:{internal_port}",
    )
    router_env = dict(
        base_env,
        ROUTER_API_URL=f"http://127.0.0.1:{internal_port}",
        ROUTER_BRIDGE_URL=f"http://127.0.0.1:{bridge_port}",
    )

    api_cmd = [
        sys.executable,
        str(REPO_ROOT / "main.py"),
        "server",
        "--port",
        str(internal_port),
    ]
    bridge_cmd = [sys.executable, str(HERE / "oai-bridge.py")]
    router_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "aistudio_api.sidecar.router:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(public_port),
        "--log-level",
        "warning",
        "--no-access-log",
        "--timeout-graceful-shutdown",
        "5",
    ]

    api_proc = subprocess.Popen(api_cmd, cwd=str(REPO_ROOT), env=api_env)
    bridge_proc = subprocess.Popen(bridge_cmd, cwd=str(REPO_ROOT), env=bridge_env)
    router_proc = subprocess.Popen(router_cmd, cwd=str(REPO_ROOT), env=router_env)

    print(
        f"[launcher] started api=:{internal_port} bridge=127.0.0.1:{bridge_port} router=0.0.0.0:{public_port}",
        flush=True,
    )

    shutting_down = False
    shutdown_rc = 0

    def _on_signal(signum, _frame):
        nonlocal shutting_down
        if not shutting_down:
            print(f"[launcher] received signal {signum}; shutting down", flush=True)
            shutting_down = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    bridge_restarts: list[float] = []

    try:
        while not shutting_down:
            if api_proc.poll() is not None:
                print(
                    f"[launcher] aistudio-api exited unexpectedly ({api_proc.returncode})",
                    flush=True,
                )
                shutdown_rc = api_proc.returncode or 1
                shutting_down = True
                break
            if router_proc.poll() is not None:
                print(
                    f"[launcher] router exited unexpectedly ({router_proc.returncode})",
                    flush=True,
                )
                shutdown_rc = router_proc.returncode or 1
                shutting_down = True
                break
            if bridge_proc.poll() is not None:
                now = time.monotonic()
                bridge_restarts = [t for t in bridge_restarts if now - t < 60.0]
                if len(bridge_restarts) >= 5:
                    print("[launcher] oai-bridge crashed too often; shutting down", flush=True)
                    shutdown_rc = 1
                    shutting_down = True
                    break
                bridge_restarts.append(now)
                print(
                    f"[launcher] oai-bridge exited ({bridge_proc.returncode}); restarting",
                    flush=True,
                )
                bridge_proc = subprocess.Popen(bridge_cmd, cwd=str(REPO_ROOT), env=bridge_env)
            time.sleep(0.25)
    finally:
        # Stop router first so no new requests arrive, and immediately send SIGTERM
        # to aistudio-api so it gets the full grace period for state_sync.stop().
        if api_proc.poll() is None:
            try:
                api_proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        _terminate_wait(router_proc, "router", timeout=4.0)
        _terminate_wait(bridge_proc, "oai-bridge", timeout=3.0)
        api_rc = _terminate_wait(api_proc, "aistudio-api", timeout=28.0)
        if shutdown_rc == 0 and api_rc is not None:
            shutdown_rc = api_rc

    return shutdown_rc


if __name__ == "__main__":
    raise SystemExit(main())
