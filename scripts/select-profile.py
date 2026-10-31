#!/usr/bin/env python3
"""Select one managed development-cache profile for this host from a verified CI artifact.

usage:
  select-profile.py --artifact DIR --base-profile JSON [--retention SECONDS]
                    [--collector JSON ...] [--manager-path] [--apply]
  select-profile.py --rollback RECEIPT [--apply]

The artifact is the extracted `nds-cache-native-<os>-<arch>` CI output: its
SHA256SUMS must cover exactly its regular files. The execution binary serves
the frontends and the maintenance binary runs collection. One new profile JSON
(a new file, never an edit of a live one) copies the base profile's tool and
root paths, sets `retention_seconds` and points `frontends` at the new
argv-preserving wrappers. The shell selector and the scheduler (a systemd user
service/timer on Linux, a launchd agent on macOS) then name that one profile.
Additional `--collector` configs keep collecting retained legacy roots.

Without --apply nothing is written; the plan is printed. --apply backs up every
replaced file under ~/.local/state/nds/cleaner-selection/<stamp>/ and writes a
receipt that --rollback restores. Installed binaries and the new profile are
left in place on rollback; nothing is deleted.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

LABEL = "ai.nddev.nds.development-cache"
UNIT = "nds-development-cache"
HEX = re.compile(r"[0-9a-f]{40}")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_artifact(directory):
    directory = directory.resolve(strict=True)
    sums = directory / "SHA256SUMS"
    listed = {}
    for line in sums.read_text().splitlines():
        digest, name = line.split("  ", 1)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or name in listed or name.startswith("/") or ".." in name.split("/"):
            raise ValueError("artifact SHA256SUMS is malformed")
        listed[name] = digest
    present = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() or p.is_symlink()}
    if present != set(listed) | {"SHA256SUMS"}:
        raise ValueError("artifact files differ from SHA256SUMS")
    for name, digest in listed.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"artifact digest mismatch: {name}")
    roles = {}
    for role in ("execution", "maintenance"):
        source = (directory / role / "SOURCE_COMMIT").read_text().strip()
        if not HEX.fullmatch(source):
            raise ValueError(f"{role} source is not a full commit")
        roles[role] = {"source": source, "binary": directory / role / "nds-cleaner-cache",
                       "sha256": listed[f"{role}/nds-cleaner-cache"]}
    return {"directory": directory, "roles": roles, "frontend_installer": directory / "install-cache-frontends.py"}


def paths(home, profile_id, roles):
    share = home / ".local/share/nds"
    return {
        "binary": {role: share / "cleaner" / data["source"] / "nds-cleaner-cache" for role, data in roles.items()},
        "profile": home / ".config/nds/development-cache" / f"{profile_id}.json",
        "frontends": share / "profiles" / profile_id / "bin",
        "selector": home / ".config/nds/development-profile.sh",
        "service": home / ".config/systemd/user" / f"{UNIT}.service",
        "timer": home / ".config/systemd/user" / f"{UNIT}.timer",
        "plist": home / "Library/LaunchAgents" / f"{LABEL}.plist",
        "state": home / ".local/state/nds/cleaner-selection",
    }


def render_selector(profile_path, profile_sha, execution, execution_sha, frontends, rustup_home):
    lines = [
        "# NDS development tools; private generated binding",
        "# Inherit bindings maintained by the native network-cache client.",
        'if [ -r "$HOME/.config/nds-cache/environment.sh" ]; then',
        '  . "$HOME/.config/nds-cache/environment.sh"',
        "fi",
        f"export NDS_CLEANER_CONFIG={shlex.quote(str(profile_path))}",
        f"export NDS_CLEANER_BIN={shlex.quote(str(execution))}",
        f"export NDS_CACHE_FRONTENDS={shlex.quote(str(frontends))}",
        f"nds_profile_clean={shlex.quote(str(frontends))}",
        'nds_profile_tail="$PATH"',
        "while :; do",
        "  nds_profile_part=${nds_profile_tail%%:*}",
        '  case "$nds_profile_part" in',
        '    "$NDS_CACHE_FRONTENDS"|*/.local/share/nds/profiles/*/bin) ;;',
        '    *) nds_profile_clean="$nds_profile_clean:$nds_profile_part" ;;',
        "  esac",
        '  case "$nds_profile_tail" in *:*) nds_profile_tail=${nds_profile_tail#*:} ;; *) break ;; esac',
        "done",
        'export PATH="$nds_profile_clean"',
        "unset nds_profile_clean nds_profile_tail nds_profile_part",
        f"export NDS_CLEANER_BIN_SHA256={execution_sha}",
        f"export NDS_CLEANER_CONFIG_SHA256={profile_sha}",
    ]
    if rustup_home:
        lines.append(f"export RUSTUP_HOME={shlex.quote(str(rustup_home))}")
    return "\n".join(lines) + "\n"


def render_service(maintenance, configs):
    starts = "\n".join(f"ExecStart={shlex.join([str(maintenance), str(c), 'clean', '--apply', '--passes', '32'])}"
                       for c in configs)
    return ("[Unit]\nDescription=NDS development cache maintenance\n[Service]\nType=oneshot\n"
            f"{starts}\nTimeoutStartSec=5820\nNice=10\nIOSchedulingClass=idle\n")


TIMER = ("[Unit]\nDescription=Hourly NDS cache maintenance\n[Timer]\nOnCalendar=hourly\nPersistent=true\n"
         "[Install]\nWantedBy=timers.target\n")


def render_plist(maintenance, config, home):
    from xml.sax.saxutils import escape
    args = "".join(f"\n\t\t<string>{escape(str(a))}</string>"
                   for a in [maintenance, config, "clean", "--apply", "--passes", "32"])
    log = home / ".local/share/nds"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"\t<key>Label</key>\n\t<string>{LABEL}</string>\n"
            f"\t<key>ProgramArguments</key>\n\t<array>{args}\n\t</array>\n"
            "\t<key>RunAtLoad</key>\n\t<false/>\n"
            f"\t<key>StandardErrorPath</key>\n\t<string>{escape(str(log / 'development-cache-error.log'))}</string>\n"
            f"\t<key>StandardOutPath</key>\n\t<string>{escape(str(log / 'development-cache.log'))}</string>\n"
            "\t<key>StartInterval</key>\n\t<integer>3600</integer>\n</dict>\n</plist>\n")


def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        pending = Path(stream.name)
        stream.write(content)
    pending.chmod(mode)
    pending.replace(path)


def plan(args, run):
    home = Path(args.home).resolve()
    artifact = verify_artifact(Path(args.artifact))
    roles = artifact["roles"]
    base_path = Path(args.base_profile).resolve(strict=True)
    base = json.loads(base_path.read_text())
    if base.get("schema") != 1 or not Path(base["root"]).is_absolute():
        raise ValueError("base profile is not a schema-1 managed profile")
    if not 86400 <= args.retention <= 31536000 or args.retention % 86400:
        raise ValueError("retention must be whole days from 86400 through 31536000 seconds")
    profile_id = f"{roles['execution']['source'][:12]}-{roles['maintenance']['source'][:12]}-r{args.retention}"
    p = paths(home, profile_id, roles)
    profile = dict(base, retention_seconds=args.retention, frontends=str(p["frontends"]))
    profile_text = json.dumps(profile, indent=1) + "\n"
    if p["profile"].exists() and p["profile"].read_text() != profile_text:
        raise ValueError("a different profile already uses this identity")
    collectors = [Path(c).resolve(strict=True) for c in args.collector]
    for c in collectors:
        data = json.loads(c.read_text())
        if data.get("schema") != 1 or data.get("root") == profile["root"]:
            raise ValueError(f"collector {c} must be a distinct schema-1 root")
    system = platform.system()
    maintenance = p["binary"]["maintenance"]
    files = {"selector": render_selector(p["profile"], hashlib.sha256(profile_text.encode()).hexdigest(),
                                         p["binary"]["execution"], roles["execution"]["sha256"], p["frontends"],
                                         profile.get("rustup_home"))}
    if system == "Linux":
        files["service"] = render_service(maintenance, [p["profile"], *collectors])
        files["timer"] = TIMER
    elif system == "Darwin":
        if collectors:
            raise ValueError("launchd selection supports one collected root")
        files["plist"] = render_plist(maintenance, p["profile"], home)
    else:
        raise ValueError("profile selection is qualified for Linux and macOS only")
    manager_path = None
    if args.manager_path and system == "Linux":
        current = run(["systemctl", "--user", "show-environment"]).stdout
        line = next((l for l in current.splitlines() if l.startswith("PATH=")), None)
        if line:
            parts = [x for x in line[5:].split(":") if not re.search(r"/\.local/share/nds/profiles/[^/]+/bin$", x)]
            manager_path = {"before": line[5:], "after": ":".join([str(p["frontends"]), *parts])}
    return {"home": home, "artifact": artifact, "profile_id": profile_id, "paths": p, "profile_text": profile_text,
            "files": files, "collectors": collectors, "system": system, "manager_path": manager_path,
            "base": base_path}


def apply(selection, run):
    p, roles = selection["paths"], selection["artifact"]["roles"]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state = p["state"] / stamp
    state.mkdir(mode=0o700, parents=True)
    for role, data in roles.items():
        target = p["binary"][role]
        if target.exists():
            if sha256(target) != data["sha256"]:
                raise ValueError(f"installed {role} binary differs from the artifact")
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            pending = target.with_name(".nds-cleaner-cache.pending")
            shutil.copyfile(data["binary"], pending)
            pending.chmod(0o755)
            if sha256(pending) != data["sha256"]:
                raise ValueError(f"{role} copy differs from the artifact")
            pending.replace(target)
    if not p["profile"].exists():
        atomic_write(p["profile"], selection["profile_text"], 0o600)
    run([sys.executable, str(selection["artifact"]["frontend_installer"]), str(p["profile"]), str(p["binary"]["execution"])])
    previous = {}
    for key, content in selection["files"].items():
        target = p[key]
        if target.exists():
            backup = state / f"{key}.previous"
            shutil.copy2(target, backup)
            previous[key] = {"path": str(target), "backup": str(backup), "sha256": sha256(target)}
        else:
            previous[key] = {"path": str(target), "backup": None, "sha256": None}
        atomic_write(target, content, 0o600 if key == "selector" else 0o644)
    reload(selection["system"], p, run)
    if selection["manager_path"]:
        run(["systemctl", "--user", "set-environment", "PATH=" + selection["manager_path"]["after"]])
    preview = run([str(p["binary"]["maintenance"]), str(p["profile"]), "clean"]).stdout
    receipt = {
        "schema": "nds-cleaner-selection/v1", "at": stamp, "host": platform.node(), "system": selection["system"],
        "profile_id": selection["profile_id"], "profile": str(p["profile"]), "profile_sha256": sha256(p["profile"]),
        "retention_seconds": json.loads(selection["profile_text"])["retention_seconds"],
        "root": json.loads(selection["profile_text"])["root"], "base_profile": str(selection["base"]),
        "roles": {role: {"source": d["source"], "sha256": d["sha256"], "path": str(p["binary"][role])}
                  for role, d in roles.items()},
        "frontends": str(p["frontends"]), "collectors": [str(c) for c in selection["collectors"]],
        "replaced": previous, "manager_path": selection["manager_path"],
        "first_preview": json.loads(preview) if preview.strip().startswith("{") else preview.strip()[:2000],
    }
    atomic_write(state / "receipt.json", json.dumps(receipt, indent=1) + "\n", 0o600)
    return state / "receipt.json", receipt


def reload(system, p, run):
    if system == "Linux":
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
    else:
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
        run(["launchctl", "bootstrap", domain, str(p["plist"])])


def rollback(receipt_path, run, write):
    receipt = json.loads(Path(receipt_path).read_text())
    actions = []
    for key, entry in receipt["replaced"].items():
        target = Path(entry["path"])
        if entry["backup"]:
            actions.append(("restore", key, str(target)))
            if write:
                atomic_write(target, Path(entry["backup"]).read_text(), 0o600 if key == "selector" else 0o644)
        else:
            actions.append(("remove-new", key, str(target)))
            if write and target.exists():
                target.unlink()
    if write:
        home = Path(receipt["profile"]).parents[3]
        reload(receipt["system"], paths(home, receipt["profile_id"], {})
               | {"plist": Path(receipt["replaced"].get("plist", {}).get("path", home / "Library/LaunchAgents" / f"{LABEL}.plist"))},
               run)
        if receipt.get("manager_path"):
            run(["systemctl", "--user", "set-environment", "PATH=" + receipt["manager_path"]["before"]])
    return actions


def runner(command, check=True):
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=1800)


def main(argv=None, run=runner):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifact")
    parser.add_argument("--base-profile")
    parser.add_argument("--retention", type=int, default=86400)
    parser.add_argument("--collector", action="append", default=[])
    parser.add_argument("--manager-path", action="store_true",
                        help="replace managed frontend prefixes in the systemd user manager PATH (Linux)")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--rollback")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.rollback:
        print(json.dumps({"rollback": rollback(args.rollback, run, args.apply), "applied": args.apply}, indent=1))
        return 0
    if not args.artifact or not args.base_profile:
        parser.error("--artifact and --base-profile are required")
    selection = plan(args, run)
    summary = {"profile_id": selection["profile_id"], "system": selection["system"],
               "profile": str(selection["paths"]["profile"]), "frontends": str(selection["paths"]["frontends"]),
               "roles": {r: d["source"] for r, d in selection["artifact"]["roles"].items()},
               "writes": sorted(selection["files"]), "collectors": [str(c) for c in selection["collectors"]],
               "manager_path": selection["manager_path"], "applied": False}
    if not args.apply:
        print(json.dumps(summary, indent=1))
        return 0
    receipt_path, _ = apply(selection, run)
    print(json.dumps(dict(summary, applied=True, receipt=str(receipt_path)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
