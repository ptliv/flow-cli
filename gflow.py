"""Antigravity / CLI entrypoint for Google Flow via Dachapgo Browser (다챱고 브라우저 v1.0.3, :50330).

Subcommands added on top of `gflow`:
  - `gflow dachapgo-status` : Show local Dachapgo `:50330` server status + configured Google profiles.
  - `gflow dachapgo-stop <profile>` : Safely stop a Dachapgo browser profile and vault cookies.
  - `gflow rotate run|batch|status|reset` : Multi-account rotation (see flow_rotate.py).
  - All standard `gflow` commands (`image t2i`, `image i2i`, `video t2v`, `video i2v`,
    `video r2v`, `video extend`, `credits user`, `project list`, `auth list`, `mcp`, etc.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM_SRC = ROOT / "_upstream" / "gflow-cli" / "src"
if str(UPSTREAM_SRC) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_SRC))


def _handle_dachapgo_custom(argv: list[str]) -> int | None:
    if not argv:
        return None
    cmd = argv[0].strip().lower()
    if cmd == "dachapgo-status":
        from gflow_cli.dachapgo_bridge import (
            dachapgo_server_status,
            list_dachapgo_google_profiles,
        )

        srv = dachapgo_server_status()
        profiles = []
        for p in list_dachapgo_google_profiles():
            profiles.append(
                {
                    "profile": p["name"],
                    "user_id": p["user_id"],
                    "label": p["label"],
                    "google_account": p["google_account"],
                    "cookies_present": p["cookies_present"],
                    "is_default": p["is_default"],
                    "last_used_at": p["last_used_at"].isoformat() if p["last_used_at"] else None,
                    "profile_dir": str(p["profile_dir"]),
                }
            )
        payload = {
            "dachapgo_server": {
                "status": srv.get("status"),
                "version": srv.get("version"),
                "engine": srv.get("engine"),
                "api_port": srv.get("api_port", 50330),
                "active_browsers": srv.get("active_browsers"),
                "max_active_browsers": srv.get("max_active_browsers"),
                "general_browser_cap": srv.get("general_browser_cap"),
                "reserved_for_priority": srv.get("reserved_for_priority"),
            },
            "google_profiles": profiles,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    if cmd == "dachapgo-stop":
        from gflow_cli.dachapgo_bridge import (
            resolve_dachapgo_uid_and_serial,
            stop_dachapgo_browser,
        )

        if len(argv) < 2:
            sys.stderr.write("Usage: gflow dachapgo-stop <profile_serial_or_uid>\n")
            return 2
        target = argv[1]
        resolved = resolve_dachapgo_uid_and_serial(target)
        if not resolved:
            sys.stderr.write(f"Unknown Dachapgo profile: {target}\n")
            return 2
        uid, serial = resolved
        res = stop_dachapgo_browser(uid)
        sys.stdout.write(
            json.dumps(
                {"status": "stopped", "profile": serial, "user_id": uid, "result": res},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0

    return None


def main() -> None:
    if sys.argv[1:2] == ["rotate"]:
        sys.path.insert(0, str(ROOT))
        from flow_rotate import main as rotate_main

        sys.exit(rotate_main(sys.argv[2:]))
    custom_rc = _handle_dachapgo_custom(sys.argv[1:])
    if custom_rc is not None:
        sys.exit(custom_rc)
    from gflow_cli.cli import main as gflow_main

    gflow_main()


if __name__ == "__main__":
    main()
