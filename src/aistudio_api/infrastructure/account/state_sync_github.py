"""Persist the accounts directory to a private GitHub repo (Render free plan).

Render's free plan has no persistent disk, so ``data/accounts`` (registry +
per-account ``auth.json`` whose cookies the proxy rotates every ~7 minutes) is
wiped on every restart/redeploy/sleep. This module keeps one bundle file in a
private GitHub repo via the Contents API:

* ``restore_accounts_from_github()`` - called at startup *before*
  ``AccountStore()`` is created: downloads the bundle and writes the files into
  the accounts dir (only when the local registry has no accounts).
* ``GitHubStateSync`` - background task: every ``AISTUDIO_STATE_GH_INTERVAL``
  seconds (default 60) hashes the accounts dir and PUTs the bundle only when it
  changed (rotation save, import, delete, tier update...). A final push runs on
  shutdown (Render sends SIGTERM before replacing an instance).

Enabled only when both ``AISTUDIO_STATE_GH_TOKEN`` and ``AISTUDIO_STATE_GH_REPO``
(``owner/name``) are set. Optional: ``AISTUDIO_STATE_GH_BRANCH`` (default
``main``), ``AISTUDIO_STATE_GH_PATH``
(prefix, default ``state/``; bundle = ``<prefix>accounts.json``),
``AISTUDIO_STATE_GH_INTERVAL`` (seconds, default 60).

Never raises into the app, never logs cookie values or the token.
Stdlib only (urllib), so no new dependency.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("state_sync")

BUNDLE_NAME = "accounts.json"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_TIMEOUT = 20.0


def _accounts_dir() -> Path:
    from aistudio_api.infrastructure.account.account_store import (
        _resolve_accounts_dir,
    )

    return _resolve_accounts_dir()


class _Config:
    def __init__(self) -> None:
        self.token = (os.getenv("AISTUDIO_STATE_GH_TOKEN") or "").strip()
        self.repo = (os.getenv("AISTUDIO_STATE_GH_REPO") or "").strip().strip("/")
        self.branch = (os.getenv("AISTUDIO_STATE_GH_BRANCH") or "main").strip()
        prefix = os.getenv("AISTUDIO_STATE_GH_PATH")
        prefix = "state/" if prefix is None else prefix.strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.path = f"{prefix}{BUNDLE_NAME}"
        self.api = (
            os.getenv("AISTUDIO_STATE_GH_API") or "https://api.github.com"
        ).rstrip("/")
        try:
            self.interval = max(15.0, float(os.getenv("AISTUDIO_STATE_GH_INTERVAL") or 60))
        except ValueError:
            self.interval = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo and "/" in self.repo)

    @property
    def url(self) -> str:
        return f"{self.api}/repos/{self.repo}/contents/{self.path}"


def is_enabled() -> bool:
    return _Config().enabled


# ---------------------------------------------------------------- bundle I/O


def _is_syncable(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return not (
        ".tmp." in name
        or name.endswith(".tmp")
        or "__pycache__" in rel
        or name.startswith(".")
    )


def collect_files(base: Path) -> dict[str, str]:
    """Return {relative_posix_path: base64(content)} for the accounts dir."""
    files: dict[str, str] = {}
    if not base.is_dir():
        return files
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if not _is_syncable(rel):
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
            files[rel] = base64.b64encode(p.read_bytes()).decode("ascii")
        except OSError:
            continue  # file vanished mid-scan (atomic replace); next poll
    return files


def files_digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(files[rel].encode())
        h.update(b"\0")
    return h.hexdigest()


def _registry_account_count(files: dict[str, str]) -> int:
    raw = files.get("registry.json")
    if not raw:
        return 0
    try:
        data = json.loads(base64.b64decode(raw))
        accs = data.get("accounts") if isinstance(data, dict) else None
        return len(accs) if isinstance(accs, (dict, list)) else 0
    except Exception:
        return 0


def _safe_target(base: Path, rel: str) -> Path | None:
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def write_files(base: Path, files: dict[str, str]) -> int:
    base.mkdir(parents=True, exist_ok=True)
    written = 0
    for rel, b64 in files.items():
        target = _safe_target(base, rel)
        if target is None or not _is_syncable(rel):
            logger.warning("State restore: skip unsafe path %r", rel)
            continue
        data = base64.b64decode(b64)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".restore.{os.getpid()}")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        written += 1
    return written


# ---------------------------------------------------------------- GitHub API


class _GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _request(cfg: _Config, method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg.token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "aistudio-api-state-sync")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            payload = json.loads(e.read() or b"{}")
            msg = str(payload.get("message", ""))[:200]
        except Exception:
            pass
        raise _GitHubError(e.code, msg or e.reason or "error") from None


def _get_bundle(cfg: _Config) -> tuple[dict | None, str | None]:
    """Return (bundle, sha); (None, None) when the file does not exist yet."""
    url = f"{cfg.url}?ref={urllib.request.quote(cfg.branch)}"
    try:
        meta = _request(cfg, "GET", url)
    except _GitHubError as e:
        if e.status == 404:
            return None, None
        raise
    sha = meta.get("sha")
    content = meta.get("content")
    if not content and meta.get("git_url"):  # >1MB files: fetch blob
        blob = _request(cfg, "GET", meta["git_url"])
        content = blob.get("content", "")
    bundle = json.loads(base64.b64decode(content or ""))
    if not isinstance(bundle, dict):
        raise ValueError("bundle is not a JSON object")
    return bundle, sha


def _put_bundle(cfg: _Config, files: dict[str, str], sha: str | None) -> str | None:
    bundle = {"version": 1, "saved_at": int(time.time()), "files": files}
    body: dict = {
        "message": f"state: accounts ({_registry_account_count(files)})",
        "content": base64.b64encode(
            json.dumps(bundle, separators=(",", ":")).encode()
        ).decode("ascii"),
        "branch": cfg.branch,
    }
    if sha:
        body["sha"] = sha
    resp = _request(cfg, "PUT", cfg.url, body)
    return (resp.get("content") or {}).get("sha")


# ---------------------------------------------------------------- restore


_last_synced_digest: str | None = None
_remote_sha: str | None = None
# Set when the startup restore could not read the repo (network/auth error):
# pushing then could overwrite a good remote bundle with a stale env bootstrap,
# so pushes stay off for this process (a restart retries the restore).
_push_blocked = False


def restore_accounts_from_github() -> int:
    """Startup restore. Returns number of files written. Never raises."""
    global _last_synced_digest, _remote_sha, _push_blocked
    cfg = _Config()
    if not cfg.enabled:
        return 0
    try:
        base = _accounts_dir()
        local = collect_files(base)
        last_err: Exception | None = None
        for delay in (0, 2, 5):
            time.sleep(delay)
            try:
                bundle, sha = _get_bundle(cfg)
                last_err = None
                break
            except _GitHubError as e:
                last_err = e
                if e.status in (401, 403):
                    break  # bad token/permissions: retrying won't help
            except (OSError, ValueError) as e:
                last_err = e
        if last_err is not None:
            raise last_err
        _remote_sha = sha
        if bundle is None:
            logger.info(
                "State restore: no bundle at %s:%s yet (first run)", cfg.repo, cfg.path
            )
            return 0
        files = bundle.get("files") or {}
        if not isinstance(files, dict):
            raise ValueError("bundle.files is not an object")
        remote_digest = files_digest(files)
        if _registry_account_count(local) > 0:
            logger.info(
                "State restore skipped: %d local account(s) already present",
                _registry_account_count(local),
            )
            _last_synced_digest = remote_digest if files_digest(local) == remote_digest else None
            return 0
        written = write_files(base, files)
        _last_synced_digest = files_digest(collect_files(base))
        logger.info(
            "State restored from GitHub: files=%d, accounts=%d, saved_at=%s",
            written,
            _registry_account_count(files),
            bundle.get("saved_at"),
        )
        return written
    except Exception as e:
        _push_blocked = True
        logger.warning(
            "State restore from GitHub failed (%s: %s); pushes disabled until "
            "next restart to avoid overwriting the saved state",
            type(e).__name__,
            e,
        )
        return 0


# ---------------------------------------------------------------- push


def push_if_changed(force: bool = False) -> bool:
    """Push the bundle when the accounts dir changed. Returns True if pushed."""
    global _last_synced_digest, _remote_sha
    cfg = _Config()
    if not cfg.enabled or _push_blocked:
        return False
    try:
        files = collect_files(_accounts_dir())
        digest = files_digest(files)
        if not force and digest == _last_synced_digest:
            return False
        if _last_synced_digest is None and _registry_account_count(files) == 0:
            # Fresh, empty instance (e.g. restore failed): never overwrite a
            # possibly good remote bundle with an empty one.
            return False
        for attempt in range(3):
            try:
                _remote_sha = _put_bundle(cfg, files, _remote_sha)
                break
            except _GitHubError as e:
                if e.status in (409, 422) and attempt < 2:
                    # sha mismatch / missing: refresh sha and overwrite with
                    # our current state (this instance is the source of truth).
                    try:
                        _, _remote_sha = _get_bundle(cfg)
                    except _GitHubError as ge:
                        if ge.status != 404:
                            raise
                        _remote_sha = None
                    continue
                raise
        _last_synced_digest = digest
        logger.info(
            "State pushed to GitHub: files=%d, accounts=%d",
            len(files),
            _registry_account_count(files),
        )
        return True
    except Exception as e:
        logger.warning("State push to GitHub failed: %s: %s", type(e).__name__, e)
        return False


class GitHubStateSync:
    """Background poller started from the FastAPI lifespan."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        cfg = _Config()
        if not cfg.enabled:
            return
        logger.info(
            "GitHub state sync enabled: repo=%s branch=%s path=%s interval=%ss",
            cfg.repo,
            cfg.branch,
            cfg.path,
            int(cfg.interval),
        )
        self._task = asyncio.create_task(self._loop(cfg.interval))

    async def _loop(self, interval: float) -> None:
        await asyncio.sleep(min(interval, 20.0))
        while True:
            try:
                await asyncio.to_thread(push_if_changed)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - push_if_changed never raises
                logger.warning("State sync loop error: %s", e)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        try:
            await asyncio.wait_for(asyncio.to_thread(push_if_changed), timeout=25)
        except Exception as e:
            logger.warning("Final state push skipped: %s", type(e).__name__)
