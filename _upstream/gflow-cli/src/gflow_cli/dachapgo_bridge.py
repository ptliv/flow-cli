"""Dachapgo Browser (다챱고 브라우저 v1.0.3+, :50330) CDP bridge for gflow-cli.

Routes Google Flow browser automation through the local Dachapgo API server
(`http://127.0.0.1:50330`) so every Google Flow session runs inside a real
Dachapgo Fingerprint Chromium instance with:
  - Deterministic per-profile `--fingerprint=<seed>`
  - `_fp_ext` DOMRectReadOnly / Error.stack / Client Hints protection
  - `__Secure-1PSIDTS` rotating Google session cookie vault protection
  - Per-profile proxy / WebRTC leak lock & 12-window capacity management
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_DACHAPGO_URL = "http://127.0.0.1:50330"
DEFAULT_STUDIO_DATA_DIR = Path(
    os.environ.get("DACHAPGO_DEFAULT_DATA_DIR") or (Path.home() / ".dachapgo" / "data")
)

# Pre-registered Google seed profiles in Dachapgo (serial_number -> user_id)
def _load_seed_profiles() -> dict[str, str]:
    """Parse `DACHAPGO_GOOGLE_PROFILES` ("serial:user_id,serial:user_id")."""
    raw = os.environ.get("DACHAPGO_GOOGLE_PROFILES", "").strip()
    out: dict[str, str] = {}
    for item in raw.split(","):
        if ":" in item:
            serial, uid = item.split(":", 1)
            if serial.strip() and uid.strip():
                out[serial.strip()] = uid.strip()
    return out


GOOGLE_SEED_SERIAL_TO_UID: dict[str, str] = _load_seed_profiles()
GOOGLE_SEED_UID_TO_SERIAL: dict[str, str] = {v: k for k, v in GOOGLE_SEED_SERIAL_TO_UID.items()}
DEFAULT_GOOGLE_PROFILE = os.environ.get("DACHAPGO_DEFAULT_PROFILE") or next(iter(GOOGLE_SEED_SERIAL_TO_UID), "")


@dataclasses.dataclass
class DachapgoSession:
    """Active CDP attachment to a Dachapgo browser window."""

    user_id: str
    serial_number: str
    debug_port: int
    browser: Any
    context: Any
    already_active: bool
    used_priority_slot: bool
    created_pages: list[Any] = dataclasses.field(default_factory=list)


def dachapgo_base_url() -> str:
    return (
        os.environ.get("DACHAPGO_BASE_URL")
        or os.environ.get("ADSPOWER_BASE_URL")
        or DEFAULT_DACHAPGO_URL
    ).rstrip("/")


def studio_data_dir() -> Path:
    env_dir = os.environ.get("DACHAPGO_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_STUDIO_DATA_DIR


def load_dachapgo_api_key() -> str:
    for env_name in ("DACHAPGO_API_KEY", "ADSPOWER_API_KEY"):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    settings_file = studio_data_dir() / "adspower.json"
    if settings_file.is_file():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("api_key"):
                return str(data["api_key"]).strip()
        except Exception:
            pass
    return ""


def read_dachapgo_ledger() -> dict[str, Any]:
    ledger_path = studio_data_dir() / "fp_chromium_ledger.json"
    if not ledger_path.is_file():
        return {}
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_dachapgo_uid_and_serial(identifier: str | None) -> tuple[str, str] | None:
    """Resolve a profile name/serial/user_id (e.g. '101', 'dcg_101', '<user_id>')
    to `(user_id, serial_number)`, or None if not a Dachapgo profile.
    """
    raw = str(identifier or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("dcg_"):
        raw = raw[4:].strip()
    if raw in GOOGLE_SEED_SERIAL_TO_UID:
        return GOOGLE_SEED_SERIAL_TO_UID[raw], raw
    if raw in GOOGLE_SEED_UID_TO_SERIAL:
        return raw, GOOGLE_SEED_UID_TO_SERIAL[raw]

    ledger = read_dachapgo_ledger()
    profiles = ledger.get("profiles") or {}
    if raw in profiles and isinstance(profiles[raw], dict):
        snum = str(profiles[raw].get("serial_number") or raw)
        return raw, snum
    for uid, rec in profiles.items():
        if isinstance(rec, dict) and str(rec.get("serial_number") or "") == raw:
            return str(uid), raw
    return None


def dachapgo_profile_cache_dir(user_id: str) -> Path:
    return studio_data_dir() / "fp_chromium_cache" / f"{user_id}_fp"


def resolve_dachapgo_profile_dir(identifier: str | None) -> Path | None:
    resolved = resolve_dachapgo_uid_and_serial(identifier)
    if resolved is None:
        return None
    uid, _ = resolved
    pdir = dachapgo_profile_cache_dir(uid)
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def is_dachapgo_profile_dir(profile_dir: Path) -> bool:
    try:
        resolved = profile_dir.resolve()
    except Exception:
        resolved = profile_dir
    return resolved.name.endswith("_fp") and "fp_chromium_cache" in resolved.parts


def user_id_from_profile_dir(profile_dir: Path) -> str | None:
    if not is_dachapgo_profile_dir(profile_dir):
        return None
    return profile_dir.name[:-3]


def serial_from_user_id(user_id: str) -> str:
    if user_id in GOOGLE_SEED_UID_TO_SERIAL:
        return GOOGLE_SEED_UID_TO_SERIAL[user_id]
    ledger = read_dachapgo_ledger()
    rec = (ledger.get("profiles") or {}).get(user_id)
    if isinstance(rec, dict) and rec.get("serial_number"):
        return str(rec["serial_number"])
    return user_id


def has_google_cookies(user_id: str) -> bool:
    pdir = dachapgo_profile_cache_dir(user_id)
    vault_file = pdir / "_cookie_vault.json"
    if vault_file.is_file():
        try:
            vault = json.loads(vault_file.read_text(encoding="utf-8"))
            cookies = vault.get("cookies") or []
            if any(
                "google" in str(c.get("domain") or "")
                and c.get("name") in ("SID", "__Secure-1PSID", "__Secure-1PSIDTS", "SAPISID")
                for c in cookies
                if isinstance(c, dict)
            ):
                return True
        except Exception:
            pass
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        if (pdir / rel).is_file():
            return True
    return False


def list_dachapgo_google_profiles(default_name: str | None = None) -> list[dict[str, Any]]:
    """Return metadata dicts for all Google seed profiles in Dachapgo."""
    ledger = read_dachapgo_ledger()
    profiles = ledger.get("profiles") or {}
    eff_default = default_name or os.environ.get("GFLOW_CLI_PROFILE") or DEFAULT_GOOGLE_PROFILE
    rows: list[dict[str, Any]] = []
    for serial, uid in GOOGLE_SEED_SERIAL_TO_UID.items():
        rec = profiles.get(uid) if isinstance(profiles.get(uid), dict) else {}
        pdir = dachapgo_profile_cache_dir(uid)
        vault_file = pdir / "_cookie_vault.json"
        last_used = None
        if vault_file.is_file():
            try:
                last_used = datetime.fromtimestamp(vault_file.stat().st_mtime, tz=UTC)
            except Exception:
                pass
        acct_file = pdir / ".gflow_account"
        email = None
        if acct_file.is_file():
            try:
                email = acct_file.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass
        rows.append(
            {
                "name": serial,
                "user_id": uid,
                "serial_number": serial,
                "label": str(rec.get("name") or f"구글 인증 ({serial})"),
                "profile_dir": pdir,
                "cookies_present": has_google_cookies(uid),
                "last_used_at": last_used,
                "is_default": serial == eff_default or uid == eff_default,
                "google_account": email or str(rec.get("handle") or f"dachapgo:{serial}"),
            }
        )
    return rows


def _api_get(path: str, params: dict[str, Any] | None = None, timeout: float = 45.0) -> dict[str, Any]:
    url = f"{dachapgo_base_url()}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{qs}"
    headers = {"Accept": "application/json"}
    api_key = load_dachapgo_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def dachapgo_server_status() -> dict[str, Any]:
    res = _api_get("/status", timeout=10.0)
    return res.get("data") if isinstance(res.get("data"), dict) else {}


def start_dachapgo_browser(
    user_id: str,
    *,
    foreground: bool | None = None,
) -> dict[str, Any]:
    """Start or reuse a Dachapgo browser profile via `:50330/api/v1/browser/start`.

    Tries background mode (`foreground=0`) first so the operator's screen is not
    disturbed. If the 9 general automation slots are full (`상한(9/12개)`),
    automatically retries with `foreground=1` to utilize one of the 3 priority
    slots without failing or killing any existing windows.
    """
    if foreground is None:
        fg_env = os.environ.get("GFLOW_DACHAPGO_FOREGROUND", "0").strip().lower()
        want_fg = fg_env in ("1", "true", "yes")
    else:
        want_fg = foreground

    res = _api_get(
        "/api/v1/browser/start",
        {"user_id": user_id, "foreground": "1" if want_fg else "0"},
        timeout=95.0,
    )
    used_priority = False
    if res.get("code") != 0 and not want_fg:
        msg = str(res.get("msg") or "")
        if "상한" in msg or "capacity" in msg.lower():
            log.info(
                "dachapgo.general_slots_full_retrying_priority_slot",
                user_id=user_id,
                server_msg=msg,
            )
            res = _api_get(
                "/api/v1/browser/start",
                {"user_id": user_id, "foreground": "1"},
                timeout=95.0,
            )
            used_priority = True

    if res.get("code") != 0:
        raise RuntimeError(
            f"다챱고 브라우저 시작 실패 (user_id={user_id}): {res.get('msg') or res}"
        )

    data = res.get("data") or {}
    debug_port = int(data.get("debug_port") or 0)
    if debug_port <= 0:
        raise RuntimeError(
            f"다챱고 브라우저 응답에 debug_port가 없습니다 (API Key 확인 필요): {data}"
        )
    return {
        "user_id": user_id,
        "serial_number": serial_from_user_id(user_id),
        "debug_port": debug_port,
        "ws": data.get("ws") or {},
        "pid": data.get("pid"),
        "already_active": bool(data.get("already_active")),
        "used_priority_slot": used_priority,
    }


def stop_dachapgo_browser(user_id: str) -> dict[str, Any]:
    """Stop a Dachapgo browser via `:50330/api/v1/browser/stop` so cookies
    (including `__Secure-1PSIDTS`) are safely vaulted and the slot is freed.
    """
    try:
        res = _api_get("/api/v1/browser/stop", {"user_id": user_id}, timeout=30.0)
        return res.get("data") if isinstance(res.get("data"), dict) else {}
    except Exception as exc:
        log.warning("dachapgo.stop_browser_failed", user_id=user_id, error=str(exc))
        return {}


async def attach_dachapgo_context(pw: Any, profile_dir: Path) -> DachapgoSession:
    """Start/reuse the Dachapgo browser for `profile_dir` and attach Playwright via CDP."""
    uid = user_id_from_profile_dir(profile_dir)
    if not uid:
        raise RuntimeError(f"Not a Dachapgo profile directory: {profile_dir}")

    info = start_dachapgo_browser(uid)
    debug_port = info["debug_port"]
    cdp_url = f"http://127.0.0.1:{debug_port}"
    log.info(
        "dachapgo.connecting_cdp",
        user_id=uid,
        serial=info["serial_number"],
        cdp_url=cdp_url,
        already_active=info["already_active"],
        used_priority_slot=info["used_priority_slot"],
    )
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        raise RuntimeError(f"Connected to Dachapgo CDP ({cdp_url}) but found 0 browser contexts.")
    context = browser.contexts[0]
    return DachapgoSession(
        user_id=uid,
        serial_number=info["serial_number"],
        debug_port=debug_port,
        browser=browser,
        context=context,
        already_active=info["already_active"],
        used_priority_slot=info["used_priority_slot"],
    )


async def detach_dachapgo_session(session: DachapgoSession) -> None:
    """Cleanly release our tabs/CDP connection without killing the Dachapgo browser,
    unless `GFLOW_DACHAPGO_CLOSE_ON_DONE=1` or we borrowed a priority slot for a cold start.
    """
    close_on_done_env = os.environ.get("GFLOW_DACHAPGO_CLOSE_ON_DONE", "1").strip().lower()
    if close_on_done_env in ("0", "false", "no"):
        should_stop_browser = False
    else:
        # Stop by default if this CLI run cold-started the browser (already_active=False),
        # or if GFLOW_DACHAPGO_CLOSE_ON_DONE=force. Never kill a window that the operator
        # already had open before the CLI started unless explicitly forced.
        should_stop_browser = (not session.already_active) or (close_on_done_env == "force")

    # Close only extra tabs we opened if we are keeping the browser open
    if not should_stop_browser:
        for page in list(session.created_pages):
            try:
                if not page.is_closed() and len(session.context.pages) > 1:
                    await page.close()
            except Exception:
                pass
    else:
        log.info(
            "dachapgo.stopping_browser_on_done",
            user_id=session.user_id,
            serial=session.serial_number,
            used_priority_slot=session.used_priority_slot,
        )
        stop_dachapgo_browser(session.user_id)
