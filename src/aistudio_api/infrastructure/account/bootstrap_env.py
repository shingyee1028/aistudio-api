"""Render / public-deploy helpers: require auth and optional cookie bootstrap."""

from __future__ import annotations

import json
import os
import secrets

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.cookie_parser import parse_cookie_string
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


def bootstrap_accounts_from_env(account_service) -> int:
    """Import Cookie JSON/text from env when the accounts directory is empty.

    Intended for Render's ephemeral filesystem: after a sleep or restart,
    long-lived Google cookies are re-imported from the environment. Rotated
    short-lived cookies are not preserved unless a persistent disk is attached.
    """
    raw = _load_bootstrap_raw()
    if not raw:
        return 0
    existing = account_service.list_accounts()
    if existing:
        logger.info(
            "Skip cookie bootstrap: %d account(s) already present",
            len(existing),
        )
        return 0

    storage_state = parse_cookie_string(raw)
    cookies = storage_state.get("cookies")
    cookie_list = cookies if isinstance(cookies, list) else []
    if not cookie_list:
        # Cookie-Editor sometimes wraps a bundle object; try a thin unwrap.
        with_suppress = True
        if raw[0] in "{[":
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "cookies" in parsed:
                    storage_state = parse_cookie_string(parsed["cookies"])
                    cookies = storage_state.get("cookies")
                    cookie_list = cookies if isinstance(cookies, list) else []
            except Exception:
                pass
        if not cookie_list:
            raise RuntimeError(
                "AISTUDIO_BOOTSTRAP_COOKIES was set but no valid cookies were parsed"
            )
        _ = with_suppress

    account = account_service.save_account_from_cookies(
        name=os.getenv("AISTUDIO_BOOTSTRAP_ACCOUNT_NAME") or "Bootstrap account",
        email=None,
        storage_state=storage_state,
        auth_user=os.getenv("AISTUDIO_BOOTSTRAP_AUTH_USER") or "0",
        cookie_id=f"cookie_{secrets.token_hex(4)}",
        tier="Free",
        notes="Imported from AISTUDIO_BOOTSTRAP_COOKIES on startup",
    )
    logger.info("Bootstrapped account %s from environment cookies", account.id)
    return 1
