"""Account rotation wrapper for gflow (multi-profile Google Flow).

Usage:
  python gflow.py rotate run [--strategy most-credits|round-robin] -- image t2i "prompt" ...
  python gflow.py rotate batch prompts.txt [--strategy ...] -- video t2v {prompt} --model ...
  python gflow.py rotate status [--refresh] [--json]
  python gflow.py rotate reset [profile]

Config (env or .env):
  GFLOW_ROTATE_PROFILES   comma separated profile names (required)
  GFLOW_ROTATE_STATE_DIR  state dir (default ~/.gflow-rotate)
  GFLOW_ROTATE_CREDITS_TTL   seconds to cache credit reads (default 1800)
  GFLOW_ROTATE_DELAY / GFLOW_ROTATE_JITTER   seconds between jobs (default 5 / 3)
  GFLOW_ROTATE_MAX_ATTEMPTS  profiles tried per job (default = number of profiles)
  GFLOW_ROTATE_MIN_CREDITS   skip profiles below this (default 0)
  GFLOW_ROTATE_GFLOW_CMD     override gflow command (default: python gflow.py)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent

# gflow exit codes (see _upstream/gflow-cli/src/gflow_cli/errors.py EXIT_CODE_MAP)
EXIT_AUTH_EXPIRED = 3
EXIT_RATE_LIMIT = 4
EXIT_CONTENT_POLICY = 5
EXIT_AUTH_MISSING = 8
EXIT_WAF = 10
EXIT_AGENT_UI = 25
EXIT_FLOW_APP = 31
EXIT_HOST_MIGRATED = 36
EXIT_INSUFFICIENT_CREDITS = 37
EXIT_ACCOUNT_CHOOSER = 38
EXIT_ACCESS_UNAVAILABLE = 39

# exit code -> (reason, default cooldown seconds). Everything listed rotates to next profile.
ROTATE_CODES: dict[int, tuple[str, int]] = {
    EXIT_RATE_LIMIT: ("rate_limit", 3600),
    EXIT_INSUFFICIENT_CREDITS: ("insufficient_credits", 6 * 3600),
    EXIT_AUTH_EXPIRED: ("auth_expired", 24 * 3600),
    EXIT_AUTH_MISSING: ("auth_missing", 24 * 3600),
    EXIT_ACCOUNT_CHOOSER: ("account_chooser", 24 * 3600),
    EXIT_ACCESS_UNAVAILABLE: ("access_unavailable", 24 * 3600),
    EXIT_HOST_MIGRATED: ("host_migrated", 6 * 3600),
    EXIT_WAF: ("waf", 1800),
    EXIT_AGENT_UI: ("agent_ui_flap", 300),
    EXIT_FLOW_APP: ("flow_app_error", 300),
}
RATE_LIMIT_MARKERS = ("429", "RESOURCE_EXHAUSTED", "rate limit", "quota")


def _env_int(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], Result]


def default_runner(cmd: list[str]) -> Result:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return Result(p.returncode, p.stdout, p.stderr)


def gflow_base_cmd() -> list[str]:
    raw = os.environ.get("GFLOW_ROTATE_GFLOW_CMD", "").strip()
    if raw:
        return shlex.split(raw)
    return [sys.executable, str(ROOT / "gflow.py")]


class State:
    """JSON state (credits cache, cooldowns, rr cursor) + JSONL usage log."""

    def __init__(self, state_dir: Path | None = None, now: Callable[[], float] = time.time):
        self.dir = Path(state_dir or os.environ.get("GFLOW_ROTATE_STATE_DIR") or Path.home() / ".gflow-rotate")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.log_path = self.dir / "usage.jsonl"
        self.now = now
        self.data = {"credits": {}, "cooldown": {}, "rr_index": 0}
        if self.path.is_file():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def log(self, **rec) -> None:
        rec = {"ts": self.now(), **rec}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def usage(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.log_path.is_file():
            return out
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") != "job":
                continue
            u = out.setdefault(r["profile"], {"jobs": 0, "ok": 0, "failed": 0, "last": None})
            u["jobs"] += 1
            u["ok" if r.get("returncode") == 0 else "failed"] += 1
            u["last"] = r["ts"]
        return out

    def cooldown_until(self, profile: str) -> float:
        cd = self.data["cooldown"].get(profile)
        return float(cd["until"]) if cd and float(cd["until"]) > self.now() else 0.0

    def set_cooldown(self, profile: str, seconds: float, reason: str) -> None:
        self.data["cooldown"][profile] = {"until": self.now() + seconds, "reason": reason}
        self.save()

    def cached_credits(self, profile: str, ttl: float) -> int | None:
        c = self.data["credits"].get(profile)
        if c and self.now() - float(c["at"]) < ttl:
            return c["credits"]
        return None

    def set_credits(self, profile: str, credits: int | None) -> None:
        self.data["credits"][profile] = {"credits": credits, "at": self.now()}
        self.save()


class Rotator:
    def __init__(self, profiles: list[str], state: State, runner: Runner = default_runner,
                 sleep: Callable[[float], None] = time.sleep):
        if not profiles:
            raise SystemExit("GFLOW_ROTATE_PROFILES is empty — set it in .env (see .env.example)")
        self.profiles = profiles
        self.state = state
        self.runner = runner
        self.sleep = sleep
        self.ttl = _env_int("GFLOW_ROTATE_CREDITS_TTL", 1800)
        self.min_credits = _env_int("GFLOW_ROTATE_MIN_CREDITS", 0)

    # ---- credits -------------------------------------------------------------
    def read_credits(self, profile: str, refresh: bool = False) -> int | None:
        if not refresh:
            cached = self.state.cached_credits(profile, self.ttl)
            if cached is not None:
                return cached
        res = self.runner(gflow_base_cmd() + ["credits", "user", "--profile", profile, "--json"])
        credits: int | None = None
        if res.returncode == 0:
            try:
                payload = json.loads(res.stdout[res.stdout.find("{"):])
                if payload.get("authenticated", True):
                    credits = int(payload.get("credits"))
            except Exception:
                credits = None
        elif res.returncode in ROTATE_CODES:
            reason, secs = ROTATE_CODES[res.returncode]
            self.state.set_cooldown(profile, secs, reason)
        self.state.set_credits(profile, credits)
        return credits

    # ---- selection -----------------------------------------------------------
    def available(self, exclude: set[str]) -> list[str]:
        return [p for p in self.profiles if p not in exclude and not self.state.cooldown_until(p)]

    def pick(self, strategy: str, exclude: set[str]) -> str | None:
        cands = self.available(exclude)
        if not cands:
            return None
        if strategy == "round-robin":
            n = len(self.profiles)
            start = int(self.state.data.get("rr_index", 0))
            for i in range(n):
                p = self.profiles[(start + i) % n]
                if p in cands:
                    self.state.data["rr_index"] = (self.profiles.index(p) + 1) % n
                    self.state.save()
                    return p
            return None
        scored = []
        for p in cands:
            c = self.read_credits(p)
            if self.state.cooldown_until(p):
                continue
            if c is not None and c <= self.min_credits:
                continue
            scored.append((c if c is not None else -1, -self.profiles.index(p), p))
        if not scored:
            return None
        scored.sort(reverse=True)
        return scored[0][2]

    # ---- run -----------------------------------------------------------------
    @staticmethod
    def classify(res: Result) -> tuple[str, int] | None:
        if res.returncode == 0:
            return None
        if res.returncode in ROTATE_CODES:
            return ROTATE_CODES[res.returncode]
        text = (res.stdout + res.stderr).lower()
        if any(m.lower() in text for m in RATE_LIMIT_MARKERS):
            return ("rate_limit_text", 3600)
        return None

    def run_job(self, gflow_args: list[str], strategy: str = "most-credits") -> int:
        tried: set[str] = set()
        max_attempts = int(_env_int("GFLOW_ROTATE_MAX_ATTEMPTS", len(self.profiles)))
        last_rc = 1
        for _ in range(max_attempts):
            profile = self.pick(strategy, tried)
            if profile is None:
                sys.stderr.write("[rotate] no available profile (all cooling down / out of credits)\n")
                self.state.log(event="no_profile", args=gflow_args)
                return 75  # EX_TEMPFAIL
            tried.add(profile)
            t0 = self.state.now()
            res = self.runner(gflow_base_cmd() + gflow_args + ["--profile", profile])
            last_rc = res.returncode
            cls = self.classify(res)
            self.state.log(event="job", profile=profile, args=gflow_args, returncode=res.returncode,
                           duration=round(self.state.now() - t0, 3), rotate_reason=cls[0] if cls else None)
            if res.returncode == 0:
                # credits changed; invalidate cache so next pick re-reads lazily
                self.state.data["credits"].pop(profile, None)
                self.state.save()
                return 0
            if cls is None:
                return res.returncode  # terminal (content policy, bad args, ...) — don't burn other accounts
            reason, secs = cls
            self.state.set_cooldown(profile, secs, reason)
            sys.stderr.write(f"[rotate] {profile}: {reason} (exit {res.returncode}) -> cooldown {int(secs)}s, trying next\n")
        return last_rc

    def run_batch(self, prompts: list[str], template: list[str], strategy: str) -> int:
        delay = _env_int("GFLOW_ROTATE_DELAY", 5)
        jitter = _env_int("GFLOW_ROTATE_JITTER", 3)
        failures = 0
        for i, prompt in enumerate(prompts):
            args = [a.replace("{prompt}", prompt) for a in template]
            if not any("{prompt}" in a for a in template):
                args = template + [prompt]
            sys.stderr.write(f"[rotate] job {i + 1}/{len(prompts)}\n")
            rc = self.run_job(args, strategy)
            if rc != 0:
                failures += 1
                if rc == 75:
                    sys.stderr.write("[rotate] stopping batch: no profile available\n")
                    return 75
            if i < len(prompts) - 1:
                self.sleep(max(0.0, delay + random.uniform(0, jitter)))
        return 0 if failures == 0 else 1

    def status(self, refresh: bool = False) -> list[dict]:
        usage = self.state.usage()
        rows = []
        for p in self.profiles:
            credits = self.read_credits(p, refresh=True) if refresh else (self.state.data["credits"].get(p) or {}).get("credits")
            cd = self.state.data["cooldown"].get(p) or {}
            until = self.state.cooldown_until(p)
            rows.append({
                "profile": p,
                "credits": credits,
                "cooldown_remaining_s": int(until - self.state.now()) if until else 0,
                "cooldown_reason": cd.get("reason") if until else None,
                **usage.get(p, {"jobs": 0, "ok": 0, "failed": 0, "last": None}),
            })
        return rows


def read_prompts(path: str) -> list[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        return [json.loads(l)["prompt"] for l in text.splitlines() if l.strip()]
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def profiles_from_env() -> list[str]:
    return [p.strip() for p in os.environ.get("GFLOW_ROTATE_PROFILES", "").split(",") if p.strip()]


def main(argv: list[str] | None = None, runner: Runner = default_runner) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="gflow rotate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one gflow command on the best profile")
    r.add_argument("--strategy", choices=["most-credits", "round-robin"], default="most-credits")
    b = sub.add_parser("batch", help="run a gflow command for each prompt in a file")
    b.add_argument("file")
    b.add_argument("--strategy", choices=["most-credits", "round-robin"], default="most-credits")
    s = sub.add_parser("status", help="credits / usage / cooldown per profile")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("reset", help="clear cooldown for a profile (or all)")
    c.add_argument("profile", nargs="?")
    ns = ap.parse_args(argv)

    rot = Rotator(profiles_from_env(), State(), runner=runner)
    if ns.cmd == "run":
        if not passthrough:
            ap.error("pass the gflow command after --, e.g. rotate run -- image t2i \"a cat\"")
        return rot.run_job(passthrough, ns.strategy)
    if ns.cmd == "batch":
        if not passthrough:
            ap.error("pass a gflow command template after --, e.g. -- image t2i {prompt}")
        return rot.run_batch(read_prompts(ns.file), passthrough, ns.strategy)
    if ns.cmd == "reset":
        if ns.profile:
            rot.state.data["cooldown"].pop(ns.profile, None)
        else:
            rot.state.data["cooldown"] = {}
        rot.state.save()
        return 0
    rows = rot.status(refresh=ns.refresh)
    if ns.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print(f"{'profile':<16}{'credits':>8}{'jobs':>6}{'ok':>5}{'fail':>6}  cooldown")
        for row in rows:
            cd = f"{row['cooldown_remaining_s']}s ({row['cooldown_reason']})" if row["cooldown_remaining_s"] else "-"
            cr = "-" if row["credits"] is None else row["credits"]
            print(f"{row['profile']:<16}{cr!s:>8}{row['jobs']:>6}{row['ok']:>5}{row['failed']:>6}  {cd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
