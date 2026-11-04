import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import flow_rotate as fr  # noqa: E402


class FakeRunner:
    def __init__(self, credits=None, plan=None, outputs=None):
        self.credits = credits or {}
        self.plan = plan or {}  # profile -> list of exit codes (popped) or int
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, cmd):
        args = cmd[2:]
        self.calls.append(args)
        prof = args[args.index("--profile") + 1]
        if args[:2] == ["credits", "user"]:
            c = self.credits.get(prof, 0)
            if isinstance(c, int) and c < 0:
                return fr.Result(-c, "", "")
            return fr.Result(0, json.dumps({"authenticated": True, "credits": c}), "")
        p = self.plan.get(prof, 0)
        rc = p.pop(0) if isinstance(p, list) else p
        return fr.Result(rc, "", self.outputs.get(prof, ""))

    def jobs(self):
        return [c for c in self.calls if c[:2] != ["credits", "user"]]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GFLOW_ROTATE_GFLOW_CMD", "py gflow")
    monkeypatch.delenv("GFLOW_ROTATE_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("GFLOW_ROTATE_CREDITS_TTL", "600")
    clock = Clock()
    return tmp_path, clock


def make(env, runner, profiles=("a", "b", "c")):
    tmp, clock = env
    return fr.Rotator(list(profiles), fr.State(tmp, now=clock), runner=runner, sleep=lambda s: None)


def test_picks_most_credits(env):
    r = FakeRunner(credits={"a": 10, "b": 500, "c": 50})
    rot = make(env, r)
    assert rot.run_job(["image", "t2i", "cat"]) == 0
    assert r.jobs() == [["image", "t2i", "cat", "--profile", "b"]]


def test_credits_cached_with_ttl(env):
    r = FakeRunner(credits={"a": 10, "b": 500, "c": 50})
    rot = make(env, r)
    rot.run_job(["image", "t2i", "x"])
    rot.run_job(["image", "t2i", "y"])
    credit_calls = [c for c in r.calls if c[:2] == ["credits", "user"]]
    # 3 initial reads + 1 re-read of 'b' (invalidated after success)
    assert len(credit_calls) == 4
    env[1].t += 601
    rot.run_job(["image", "t2i", "z"])
    assert len([c for c in r.calls if c[:2] == ["credits", "user"]]) == 4 + 3


@pytest.mark.parametrize("code", [3, 4, 8, 25, 31, 36, 37, 38, 39])
def test_rotates_on_retryable_codes(env, code):
    r = FakeRunner(credits={"a": 1, "b": 500, "c": 50}, plan={"b": code})
    rot = make(env, r)
    assert rot.run_job(["video", "t2v", "x"]) == 0
    assert [j[-1] for j in r.jobs()] == ["b", "c"]
    assert rot.state.cooldown_until("b") > env[1].t
    assert rot.state.data["cooldown"]["b"]["reason"] == fr.ROTATE_CODES[code][0]


def test_rotates_on_429_text(env):
    r = FakeRunner(credits={"a": 1, "b": 500}, plan={"b": 1}, outputs={"b": "HTTP 429 RESOURCE_EXHAUSTED"})
    rot = make(env, r, profiles=("a", "b"))
    assert rot.run_job(["video", "t2v", "x"]) == 0
    assert [j[-1] for j in r.jobs()] == ["b", "a"]


def test_terminal_error_does_not_rotate(env):
    r = FakeRunner(credits={"a": 1, "b": 500}, plan={"b": 5})  # content policy
    rot = make(env, r, profiles=("a", "b"))
    assert rot.run_job(["video", "t2v", "x"]) == 5
    assert len(r.jobs()) == 1
    assert rot.state.cooldown_until("b") == 0


def test_all_cooling_down_returns_tempfail(env):
    r = FakeRunner(credits={"a": 5, "b": 5}, plan={"a": 4, "b": 37})
    rot = make(env, r, profiles=("a", "b"))
    assert rot.run_job(["video", "t2v", "x"]) == 37
    assert rot.run_job(["video", "t2v", "x"]) == 75


def test_cooldown_expires(env):
    r = FakeRunner(credits={"a": 5}, plan={"a": [25, 0]})
    rot = make(env, r, profiles=("a",))
    assert rot.run_job(["x"]) == 25
    assert rot.run_job(["x"]) == 75
    env[1].t += 301
    assert rot.run_job(["x"]) == 0


def test_min_credits_skips(env, monkeypatch):
    monkeypatch.setenv("GFLOW_ROTATE_MIN_CREDITS", "20")
    r = FakeRunner(credits={"a": 10, "b": 15})
    rot = make(env, r, profiles=("a", "b"))
    assert rot.run_job(["x"]) == 75


def test_round_robin(env):
    r = FakeRunner()
    rot = make(env, r)
    for _ in range(4):
        rot.run_job(["image", "t2i", "p"], strategy="round-robin")
    assert [j[-1] for j in r.jobs()] == ["a", "b", "c", "a"]
    assert not [c for c in r.calls if c[:2] == ["credits", "user"]]


def test_batch_and_status(env, tmp_path):
    slept = []
    r = FakeRunner(credits={"a": 100, "b": 90, "c": 80}, plan={"a": [0, 4]})
    rot = make(env, r)
    rot.sleep = slept.append
    assert rot.run_batch(["p1", "p2", "p3"], ["image", "t2i", "{prompt}", "--count", "1"], "most-credits") == 0
    jobs = r.jobs()
    assert jobs[0] == ["image", "t2i", "p1", "--count", "1", "--profile", "a"]
    assert jobs[1][-1] == "a" and jobs[2][-1] == "b"  # p2 on a hit 429-code -> b
    assert len(slept) == 2
    rows = {row["profile"]: row for row in rot.status()}
    assert rows["a"]["jobs"] == 2 and rows["a"]["ok"] == 1 and rows["a"]["failed"] == 1
    assert rows["a"]["cooldown_reason"] == "rate_limit"
    assert rows["b"]["ok"] == 2


def test_read_prompts(tmp_path):
    t = tmp_path / "p.txt"
    t.write_text("# c\nhello\n\nworld\n", encoding="utf-8")
    assert fr.read_prompts(str(t)) == ["hello", "world"]
    j = tmp_path / "p.jsonl"
    j.write_text('{"prompt": "a"}\n{"prompt": "b"}\n', encoding="utf-8")
    assert fr.read_prompts(str(j)) == ["a", "b"]


def test_cli_end_to_end_with_fake_gflow(tmp_path):
    log = tmp_path / "calls.log"
    envv = {
        **os.environ,
        "GFLOW_ROTATE_PROFILES": "p1,p2",
        "GFLOW_ROTATE_STATE_DIR": str(tmp_path / "state"),
        "GFLOW_ROTATE_GFLOW_CMD": f"{sys.executable} {ROOT / 'tests' / 'fake_gflow.py'}",
        "FAKE_GFLOW_PLAN": json.dumps({"p2": 37}),
        "FAKE_GFLOW_CREDITS": json.dumps({"p1": 10, "p2": 900}),
        "FAKE_GFLOW_LOG": str(log),
        "GFLOW_ROTATE_DELAY": "0",
        "GFLOW_ROTATE_JITTER": "0",
    }
    rc = subprocess.run([sys.executable, str(ROOT / "flow_rotate.py"), "run", "--", "video", "t2v", "hi"], env=envv).returncode
    assert rc == 0
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    jobs = [c for c in calls if c[0] == "video"]
    assert [c[-1] for c in jobs] == ["p2", "p1"]
    out = subprocess.run([sys.executable, str(ROOT / "flow_rotate.py"), "status", "--json"], env=envv,
                         capture_output=True, text=True).stdout
    rows = {r["profile"]: r for r in json.loads(out)}
    assert rows["p2"]["cooldown_reason"] == "insufficient_credits"
