#!/usr/bin/env python3
"""Exercise the installed native owner on a private no-mutation policy fixture."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if sys.platform != "linux":
    raise SystemExit("This isolated provider qualification currently requires Linux")
ROOT = Path(__file__).resolve().parent.parent
OWNER = 'cleaner'
binary = Path(os.environ["NDS_TEST_" + OWNER.upper()]).resolve(strict=True)
build = subprocess.run(["cargo", "test", "--locked", "--test", "provider", "--no-run", "--message-format=json"], cwd=ROOT, text=True, capture_output=True, check=True, timeout=180)
executables = [item["executable"] for line in build.stdout.splitlines() if (item := json.loads(line)).get("reason") == "compiler-artifact" and item.get("executable") and item.get("target", {}).get("name") == "provider"]
assert len(executables) == 1
with tempfile.TemporaryDirectory(prefix="nds-" + OWNER + "-") as temporary:
    home = Path(temporary)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["NDS_ISOLATED_PROVIDER"] = "1"
    for key, directory in [("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")]:
        (home / directory).mkdir(mode=0o700)
        env[key] = str(home / directory)
    for key in list(env):
        if key.startswith("RLDYOUR_"):
            del env[key]
    policy = home / "config" / ("rldyour-" + OWNER) / "config.toml"
    policy.parent.mkdir(mode=0o700)
    policy.write_text('roots = []\ncommand_timeout_seconds = 2\n[native_gc]\nuv = false\n[categories]\nprojects = false\nincremental = false\nhome_caches = false\ndevin_versions = false\ncargo_registry = false\ntrash = false\n')
    # Only observations are enabled. The real native owner creates its own
    # report in this fixture; no invented run receipt or real user report.
    subprocess.run([str(binary), "--config", str(policy), 'run', "--json"], env=env, stdout=subprocess.DEVNULL, check=True, timeout=30)
    def state():
        return {str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (home / "state").rglob("*") if path.is_file()}
    before = state()
    assert before, "native owner did not create its fixture report"
    subprocess.run([executables[0], "--ignored", "--nocapture"], env=env, check=True, timeout=120)
    assert before == state(), "read-only adapter changed native owner state"
print("Real isolated native provider acceptance passed; owner state unchanged")
