"""Profile selection: verified artifact, one new profile, selector, scheduler, receipt and rollback."""
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("select_profile", ROOT / "scripts/select-profile.py")
select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(select)

EXECUTION = "3f75f6a10842271af206132e74a88159ccfbdf00"
MAINTENANCE = "dd1169a9c2ad8a2ee85b7e8c5da85fa0ce2a899b"


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.home = base / "home"
        self.artifact = base / "artifact"
        for role, source in (("execution", EXECUTION), ("maintenance", MAINTENANCE)):
            (self.artifact / role).mkdir(parents=True)
            (self.artifact / role / "SOURCE_COMMIT").write_text(source + "\n")
            binary = self.artifact / role / "nds-cleaner-cache"
            binary.write_text(f"#!/bin/sh\necho '{{\"role\":\"{role}\"}}'\n")
            binary.chmod(0o755)
        (self.artifact / "install-cache-frontends.py").write_bytes((ROOT / "scripts/install-cache-frontends.py").read_bytes())
        (self.artifact / "LICENSE").write_text("license\n")
        self.write_sums()
        root = base / "managed"
        root.mkdir(mode=0o700)
        self.base = self.home / ".config/nds/development-cache/legacy.json"
        self.base.parent.mkdir(parents=True)
        self.base.write_text(json.dumps({"schema": 1, "root": str(root), "cargo": "/usr/bin/true",
                                         "rustc": "/usr/bin/true", "rustdoc": "/usr/bin/true",
                                         "frontends": str(self.home / ".local/share/nds/profiles/legacy/bin")}))
        self.selector = self.home / ".config/nds/development-profile.sh"
        self.selector.write_text("# legacy selector\n")
        self.calls = []

    def write_sums(self):
        lines = []
        for path in sorted(self.artifact.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(self.artifact).as_posix())
        (self.artifact / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    def run(self, command, check=True):
        self.calls.append(command)
        if command[0] in {"systemctl", "launchctl"}:
            stdout = "PATH=/x/.local/share/nds/profiles/legacy/bin:/usr/bin\n" if "show-environment" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")
        return subprocess.run(command, check=check, capture_output=True, text=True, timeout=60)

    def main(self, *args):
        return select.main(["--home", str(self.home), *args], run=self.run)


@unittest.skipUnless(platform.system() == "Linux", "systemd rendering is exercised on Linux")
class SelectProfile(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.temp.cleanup)

    def test_preview_writes_nothing(self):
        before = sorted(p.relative_to(self.fx.home) for p in self.fx.home.rglob("*"))
        with mock.patch("sys.stdout"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base))
        self.assertEqual(before, sorted(p.relative_to(self.fx.home) for p in self.fx.home.rglob("*")))
        self.assertFalse(any(c[0] == "systemctl" and "daemon-reload" in c for c in self.fx.calls))

    def test_tampered_artifact_is_refused(self):
        (self.fx.artifact / "maintenance/nds-cleaner-cache").write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base), "--apply")

    def test_apply_selects_one_profile_and_rollback_restores(self):
        with mock.patch("sys.stdout"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base),
                         "--manager-path", "--apply")
        profile_id = f"{EXECUTION[:12]}-{MAINTENANCE[:12]}-r86400"
        profile = self.fx.home / f".config/nds/development-cache/{profile_id}.json"
        data = json.loads(profile.read_text())
        self.assertEqual(data["retention_seconds"], 86400)
        self.assertEqual(data["frontends"], str(self.fx.home / f".local/share/nds/profiles/{profile_id}/bin"))
        self.assertEqual(json.loads(self.fx.base.read_text()).get("retention_seconds"), None)  # live base untouched
        for tool in ("cargo", "uv", "bun"):
            wrapper = (Path(data["frontends"]) / tool).read_text()
            self.assertIn(f"cleaner/{EXECUTION}/nds-cleaner-cache", wrapper)
            self.assertIn(str(profile), wrapper)
        selector = self.fx.selector.read_text()
        self.assertIn(f"NDS_CLEANER_CONFIG={profile}", selector)
        self.assertIn("*/.local/share/nds/profiles/*/bin) ;;", selector)
        service = (self.fx.home / ".config/systemd/user/nds-development-cache.service").read_text()
        self.assertIn(f"cleaner/{MAINTENANCE}/nds-cleaner-cache {profile} clean --apply --passes 32", service)
        self.assertTrue(any(c[:3] == ["systemctl", "--user", "set-environment"] and
                            c[3].startswith(f"PATH={data['frontends']}:") and "legacy" not in c[3] for c in self.fx.calls))
        receipt_path = next((self.fx.home / ".local/state/nds/cleaner-selection").glob("*/receipt.json"))
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["roles"]["maintenance"]["source"], MAINTENANCE)
        self.assertEqual(receipt["first_preview"], {"role": "maintenance"})
        with mock.patch("sys.stdout"):
            self.fx.main("--rollback", str(receipt_path), "--apply")
        self.assertEqual(self.fx.selector.read_text(), "# legacy selector\n")
        self.assertFalse((self.fx.home / ".config/systemd/user/nds-development-cache.service").exists())
        self.assertTrue(profile.exists())  # rollback never deletes the installed profile
        self.assertTrue(any(c[:3] == ["systemctl", "--user", "set-environment"] and "legacy" in c[3]
                            for c in self.fx.calls))

    def test_collector_must_differ_by_root_or_cargo_home(self):
        same = self.fx.home / "same.json"
        same.write_text(self.fx.base.read_text())
        with self.assertRaisesRegex(ValueError, "distinct schema-1 root or Cargo home"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base),
                         "--collector", str(same))
        source = self.fx.home / "source.json"
        source.write_text(json.dumps(dict(json.loads(self.fx.base.read_text()), cargo_home="/srv/cargo/home")))
        with mock.patch("sys.stdout"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base),
                         "--collector", str(source))

    def test_retention_must_be_whole_days(self):
        with self.assertRaisesRegex(ValueError, "whole days"):
            self.fx.main("--artifact", str(self.fx.artifact), "--base-profile", str(self.fx.base),
                         "--retention", "3600")


if __name__ == "__main__":
    unittest.main()
