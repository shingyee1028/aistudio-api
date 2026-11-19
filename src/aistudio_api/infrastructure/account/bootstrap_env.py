"""Render / public-deploy helpers: require auth and optional cookie bootstrap."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.cookie_parser import parse_cookie_string
from aistudio_api.infrastructure.utils.common import mask_email
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("bootstrap")


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def require_public_auth() -> None:
    """Refuse to start on a public host without a web password and API key."""
    if not _env_flag("AISTUDIO_REQUIRE_AUTH", "0"):  # entrypoint sets 1 in the container
        return

    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    web_password = (settings.web_password or "").strip()
    api_keys = set(settings.api_keys) | set(get_configured_api_keys())
    missing: list[str] = []
    if not web_password:
        missing.append("AISTUDIO_WEB_PASSWORD")
    if not api_keys:
        missing.append("AISTUDIO_API_KEY (or config.yaml api_keys)")
    if missing:
        raise RuntimeError(
            "Public deployment requires authentication, but these are missing: "
            + ", ".join(missing)
        )


def _load_bootstrap_raw() -> str:
    file_path = (os.getenv("AISTUDIO_BOOTSTRAP_COOKIES_FILE") or "").strip()
    if file_path:
        return open(file_path, encoding="utf-8").read().strip()
    return (os.getenv("AISTUDIO_BOOTSTRAP_COOKIES") or "").strip()


def _resolve_bootstrap_cookies(raw: str) -> tuple[str, dict[str, object]]:
    """Return (cookie_text, storage_state); unwraps a {"cookies": ...} bundle."""
    storage_state = parse_cookie_string(raw)
    cookies = storage_state.get("cookies")
    if isinstance(cookies, list) and cookies:
        return raw, storage_state
    if raw[:1] in "{[":
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and "cookies" in parsed:
            inner = parsed["cookies"]
            inner_text = inner if isinstance(inner, str) else json.dumps(inner)
            storage_state = parse_cookie_string(inner_text)
            cookies = storage_state.get("cookies")
            if isinstance(cookies, list) and cookies:
                return inner_text, storage_state
    raise RuntimeError(
        "AISTUDIO_BOOTSTRAP_COOKIES was set but no valid cookies were parsed"
    )


async def _probe_import(account_service, cookie_text: str, storage_state) -> int:
    """Same as the admin UI's 「递归探测多身份登录账号」 (POST /accounts/probe-import)."""
    from aistudio_api.infrastructure.account.cookie_parser import (
        probe_google_accounts_infinite,
    )

    timeout = float(os.getenv("AISTUDIO_BOOTSTRAP_PROBE_TIMEOUT") or "60")
    probed = await asyncio.wait_for(
        probe_google_accounts_infinite(cookie_text), timeout=timeout
    )
    if not probed:
        return 0
    metas = account_service.batch_import_accounts(
        probed_list=probed,
        storage_state=storage_state,
        prefix=os.getenv("AISTUDIO_BOOTSTRAP_ACCOUNT_NAME") or "Google Account",
        cookie_id=f"cookie_{secrets.token_hex(4)}",
    )
    for meta in metas:
        with contextlib.suppress(Exception):
            account_service.update_account_notes(
                meta.id, "Imported from AISTUDIO_BOOTSTRAP_COOKIES on startup"
            )
    logger.info(
        "Bootstrapped %d account(s) from environment cookies via multi-login probe: %s",
        len(metas),
        ", ".join(
            f"{m.id}(u/{m.auth_user},{mask_email(m.email) if m.email else '-'},{m.tier})"
            for m in metas
        ),
    )
    return len(metas)


async def _single_import(account_service, storage_state) -> int:
    """Fallback: one account (like the plain text import), still probing email/tier."""
    from aistudio_api.infrastructure.account.cookie_parser import (
        probe_account_benefit_tier,
        probe_account_email,
    )

    auth_user = (os.getenv("AISTUDIO_BOOTSTRAP_AUTH_USER") or "0").strip() or "0"
    raw_cookies = storage_state.get("cookies")
    cookie_dict = {
        str(c.get("name") or ""): str(c.get("value") or "")
        for c in (raw_cookies if isinstance(raw_cookies, list) else [])
        if isinstance(c, dict) and c.get("name")
    }
    email = None
    tier = "Free"
    with contextlib.suppress(Exception):
        email = await asyncio.wait_for(
            probe_account_email(cookie_dict, auth_user=auth_user), timeout=20
        )
    with contextlib.suppress(Exception):
        probed = await asyncio.wait_for(
            probe_account_benefit_tier(cookie_dict, auth_user=auth_user), timeout=20
        )
        if probed:
            tier = probed
    account = account_service.save_account_from_cookies(
        name=os.getenv("AISTUDIO_BOOTSTRAP_ACCOUNT_NAME") or "Bootstrap account",
        email=email,
        storage_state=storage_state,
        auth_user=auth_user,
        cookie_id=f"cookie_{secrets.token_hex(4)}",
        tier=tier,
        notes="Imported from AISTUDIO_BOOTSTRAP_COOKIES on startup",
    )
    if not account_service.get_active_account():
        account_service.set_active_account(account.id)
    logger.info(
        "Bootstrapped account %s (u/%s, tier=%s, email=%s) from environment cookies",
        account.id,
        auth_user,
        tier,
        "yes" if email else "unknown",
    )
    return 1


async def bootstrap_accounts_from_env_async(account_service) -> int:
    """Import Cookie JSON/text from env when the accounts directory is empty.

    Intended for Render's ephemeral filesystem: after a sleep or restart,
    long-lived Google cookies are re-imported from the environment. Rotated
    short-lived cookies are not preserved unless a persistent disk is attached.

    Mirrors the admin UI text import with 「递归探测多身份登录账号」 checked
    (u/0, u/1, ... with email + tier). If probing fails, falls back to a
    single u/0 import. Never raises.
    """
    try:
        raw = _load_bootstrap_raw()
    except Exception as e:
        logger.warning("Cookie bootstrap skipped: cannot read source: %s", e)
        return 0
    if not raw:
        return 0
    try:
        existing = account_service.list_accounts()
    except Exception as e:
        logger.warning("Cookie bootstrap skipped: cannot list accounts: %s", e)
        return 0
    if existing:
        logger.info(
            "Skip cookie bootstrap: %d account(s) already present",
            len(existing),
        )
        return 0

    try:
        cookie_text, storage_state = _resolve_bootstrap_cookies(raw)
    except Exception as e:
        logger.warning("Cookie bootstrap skipped: %s", e)
        return 0

    if _env_flag("AISTUDIO_BOOTSTRAP_PROBE", "1"):
        try:
            count = await _probe_import(account_service, cookie_text, storage_state)
            if count:
                return count
            logger.warning(
                "Cookie bootstrap: multi-login probe found no signed-in Google "
                "account (cookies may be expired/rotated); falling back to a "
                "single-account import"
            )
        except Exception as e:
            logger.warning(
                "Cookie bootstrap: multi-login probe failed (%s: %s); falling "
                "back to a single-account import",
                type(e).__name__,
                e,
            )
    try:
        return await _single_import(account_service, storage_state)
    except Exception as e:
        logger.warning("Cookie bootstrap failed: %s: %s", type(e).__name__, e)
        return 0


def bootstrap_accounts_from_env(account_service) -> int:
    """Sync entry point used by api/app.py's lifespan (unchanged call site).

    Runs the async probe on a private event loop in a worker thread, so it is
    safe to call from inside the running startup loop. Never raises.
    """
    import concurrent.futures

    def _run() -> int:
        return asyncio.run(bootstrap_accounts_from_env_async(account_service))

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cookie-bootstrap"
        ) as pool:
            return pool.submit(_run).result()
    except Exception as e:
        logger.warning("Cookie bootstrap failed: %s: %s", type(e).__name__, e)
        return 0
