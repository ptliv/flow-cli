"""Fake gflow used by tests. Behaviour from FAKE_GFLOW_PLAN (json: profile -> exit code)."""
import json, os, sys
args = sys.argv[1:]
prof = args[args.index("--profile") + 1] if "--profile" in args else None
plan = json.loads(os.environ.get("FAKE_GFLOW_PLAN", "{}"))
credits = json.loads(os.environ.get("FAKE_GFLOW_CREDITS", "{}"))
with open(os.environ["FAKE_GFLOW_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] == ["credits", "user"]:
    print(json.dumps({"profile": prof, "authenticated": True, "credits": credits.get(prof, 100)}))
    sys.exit(0)
sys.exit(int(plan.get(prof, 0)))
