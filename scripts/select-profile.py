#!/usr/bin/env python3
"""Select one managed development-cache profile for this host from a verified CI artifact.

usage:
  select-profile.py --artifact DIR --base-profile JSON [--retention SECONDS]
                    [--collector JSON ...] [--companions-from DIR ...] [--manager-path] [--apply]
  select-profile.py --rollback RECEIPT [--apply]

The artifact is the extracted `nds-cache-native-<os>-<arch>` CI output: its
SHA256SUMS must cover exactly its regular files. The execution binary serves
the frontends and the maintenance binary runs collection. One new profile JSON
(a new file, never an edit of a live one) copies the base profile's tool and
root paths, sets `retention_seconds` and points `frontends` at the new
argv-preserving wrappers. The shell selector and the scheduler (a systemd user
service/timer on Linux, a launchd agent on macOS) then name that one profile.
Additional `--collector` configs keep collecting retained legacy roots or a
separate Cargo home on the same root.

The frontend directory of the base profile, and of every `--companions-from`
directory, may also hold host toolchain shims that the estate installer placed
next to the managed frontends (interpreters, compilers, TypeScript). They are
carried into the new frontend directory so selecting a profile never drops a
command from PATH. A shim that runs through the Cleaner is retargeted to the new
execution binary and profile; any other reference to a managed profile, binary
or configuration is refused.

Without --apply nothing is written; the plan is printed. --apply backs up every
replaced file under ~/.local/state/nds/cleaner-selection/<stamp>/ and writes a
receipt that --rollback restores. Binaries are installed by content: a rebuild of
the same source with other bytes goes to cleaner/<source>-<sha256[:16]>/, so an
existing selection is never replaced in place. Installed binaries and the new profile are
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
MARKER = "# NDS managed development cache frontend\n"
MANAGED = re.compile(r"/\.local/share/nds/(cleaner|profiles)/|/\.config/nds/development-cache/")
THROUGH_CLEANER = re.compile(r"(?m)^(exec )(\S+/\.local/share/nds/cleaner/[0-9a-f]{40}(?:-[0-9a-f]{16})?/nds-cleaner-cache) "
                             r"(\S+/\.config/nds/development-cache/[^/\s]+\.json) ")


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


def binary_path(share, source, digest):
    """Where a role binary is installed. The per-source path the estate installer also uses is
    taken when free or when it already holds exactly these bytes. CI rebuilds of one source commit
    are not bit-identical (toolchain or runner image changes), so other bytes go to a
    content-addressed sibling instead of replacing a binary an existing selection runs."""
    legacy = share / "cleaner" / source / "nds-cleaner-cache"
    if not legacy.exists() and not legacy.is_symlink():
        return legacy
    if legacy.is_file() and not legacy.is_symlink() and sha256(legacy) == digest:
        return legacy
    return share / "cleaner" / f"{source}-{digest[:16]}" / "nds-cleaner-cache"


def paths(home, profile_id, roles):
    share = home / ".local/share/nds"
    return {
        "binary": {role: binary_path(share, data["source"], data["sha256"]) for role, data in roles.items()},
        "profile": home / ".config/nds/development-cache" / f"{profile_id}.json",
        "frontends": share / "profiles" / profile_id / "bin",
        "selector": home / ".config/nds/development-profile.sh",
        "service": home / ".config/systemd/user" / f"{UNIT}.service",
        "timer": home / ".config/systemd/user" / f"{UNIT}.timer",
        "plist": home / "Library/LaunchAgents" / f"{LABEL}.plist",
        "state": home / ".local/state/nds/cleaner-selection",
    }


def companions(sources, execution, profile):
    """Host toolchain shims beside the managed frontends, retargeted to `execution` and `profile`."""
    found = {}
    for source in sources:
        for entry in sorted(source.iterdir()):
            if entry.name == ".nds-cache-frontends" or entry.name.startswith("."):
                continue
            if entry.is_symlink() or not entry.is_file():
                raise ValueError(f"companion {entry} is not a regular file")
            text = entry.read_text()
            if MARKER in text:
                continue  # a managed frontend; the artifact's installer renders it again
            if not text.startswith("#!/bin/sh\n"):
                raise ValueError(f"companion {entry} is not a POSIX shell shim")
            text = THROUGH_CLEANER.sub(lambda m: m[1] + shlex.quote(str(execution)) + " "
                                       + shlex.quote(str(profile)) + " ", text)
            if MANAGED.search(THROUGH_CLEANER.sub("", text)):
                raise ValueError(f"companion {entry} names a managed path it cannot be retargeted from")
            for line in text.splitlines():
                if line.startswith("exec "):
                    target = shlex.split(line)[1]
                    if target != str(execution) and target.startswith("/") and not os.access(target, os.X_OK):
                        raise ValueError(f"companion {entry} runs a missing program {target}")
            if found.get(entry.name, (None, text))[1] != text:
                raise ValueError(f"companion {entry.name} differs between {found[entry.name][0]} and {source}")
            found.setdefault(entry.name, (source, text))
    return found


def render_selector(profile_path, profile_sha, execution, execution_sha, frontends, rustup_home, darwin=False):
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
    if darwin:
        # Fine-grained native Cargo locks exceed the default macOS soft descriptor limit.
        lines += ["nds_profile_nofile=$(ulimit -n)",
                  'case "$nds_profile_nofile" in unlimited) ;; *) [ "$nds_profile_nofile" -ge 8192 ] || ulimit -n 8192 ;; esac',
                  "unset nds_profile_nofile"]
    if rustup_home:
        lines.append(f"export RUSTUP_HOME={shlex.quote(str(rustup_home))}")
    return "\n".join(lines) + "\n"


def manager_environment(paths, profile_text, roles, run):
    """Bind only the selected profile's variables; never retain unrelated credentials."""
    current = json.loads(run(["systemctl", "--user", "show-environment", "--output=json"]).stdout)
    if not isinstance(current, dict) or not isinstance(current.get("PATH"), str):
        raise ValueError("systemd user manager must expose a string PATH")
    parts = [x for x in current["PATH"].split(":")
             if not re.search(r"/\.local/share/nds/profiles/[^/]+/bin$", x)]
    after = {
        "PATH": ":".join([str(paths["frontends"]), *parts]),
        "NDS_CLEANER_CONFIG": str(paths["profile"]),
        "NDS_CLEANER_BIN": str(paths["binary"]["execution"]),
        "NDS_CACHE_FRONTENDS": str(paths["frontends"]),
        "NDS_CLEANER_BIN_SHA256": roles["execution"]["sha256"],
        "NDS_CLEANER_CONFIG_SHA256": hashlib.sha256(profile_text.encode()).hexdigest(),
    }
    rustup_home = json.loads(profile_text).get("rustup_home")
    if rustup_home:
        after["RUSTUP_HOME"] = str(rustup_home)
    before = {key: current.get(key) for key in after}
    if any(value is not None and not isinstance(value, str) for value in before.values()):
        raise ValueError("selected manager environment values must be strings")
    return {"before": before, "after": after}


def set_manager_environment(values, run):
    present = [f"{key}={value}" for key, value in values.items() if value is not None]
    absent = [key for key, value in values.items() if value is None]
    if present:
        run(["systemctl", "--user", "set-environment", *present])
    if absent:
        run(["systemctl", "--user", "unset-environment", *absent])


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
    existing = p["frontends"] / "cargo"
    if existing.is_file() and str(p["binary"]["execution"]) not in existing.read_text():
        # Same sources rebuilt into other bytes must not rewrite a selected profile in place.
        raise ValueError("a different execution binary already serves this profile identity")
    collectors = [Path(c).resolve(strict=True) for c in args.collector]
    for c in collectors:
        data = json.loads(c.read_text())
        same = (data.get("root"), data.get("cargo_home")) == (profile["root"], profile.get("cargo_home"))
        if data.get("schema") != 1 or same:
            raise ValueError(f"collector {c} must be a distinct schema-1 root or Cargo home")
    system = platform.system()
    maintenance = p["binary"]["maintenance"]
    sources = [Path(d).resolve(strict=True) for d in args.companions_from]
    if base.get("frontends") and Path(base["frontends"]).is_dir():
        sources.insert(0, Path(base["frontends"]).resolve())
    shims = companions(dict.fromkeys(s for s in sources if s != p["frontends"]), p["binary"]["execution"], p["profile"])
    files = {"selector": render_selector(p["profile"], hashlib.sha256(profile_text.encode()).hexdigest(),
                                         p["binary"]["execution"], roles["execution"]["sha256"], p["frontends"],
                                         profile.get("rustup_home"), system == "Darwin")}
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
    environment = None
    if args.manager_path and system == "Linux":
        environment = manager_environment(p, profile_text, roles, run)
        manager_path = {key: value["PATH"] for key, value in environment.items()}
    return {"home": home, "artifact": artifact, "profile_id": profile_id, "paths": p, "profile_text": profile_text,
            "files": files, "collectors": collectors, "system": system, "manager_path": manager_path,
            "base": base_path, "companions": shims, "manager_environment": environment}


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
    carried = {}
    for name, (source, text) in selection["companions"].items():
        target = p["frontends"] / name
        if target.exists() and (target.is_symlink() or target.read_text() != text):
            raise ValueError(f"a different {name} already exists in the selected frontends")
        if not target.exists():
            atomic_write(target, text, 0o755)
        carried[name] = {"source": str(source / name), "sha256": sha256(target)}
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
    if selection["manager_environment"]:
        set_manager_environment(selection["manager_environment"]["after"], run)
    preview = run([str(p["binary"]["maintenance"]), str(p["profile"]), "clean"]).stdout
    receipt = {
        "schema": "nds-cleaner-selection/v1", "at": stamp, "host": platform.node(), "system": selection["system"],
        "profile_id": selection["profile_id"], "profile": str(p["profile"]), "profile_sha256": sha256(p["profile"]),
        "retention_seconds": json.loads(selection["profile_text"])["retention_seconds"],
        "root": json.loads(selection["profile_text"])["root"], "base_profile": str(selection["base"]),
        "roles": {role: {"source": d["source"], "sha256": d["sha256"], "path": str(p["binary"][role])}
                  for role, d in roles.items()},
        "frontends": str(p["frontends"]), "collectors": [str(c) for c in selection["collectors"]],
        "companions": carried, "replaced": previous, "manager_path": selection["manager_path"],
        "manager_environment": selection["manager_environment"],
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
        if receipt.get("manager_environment"):
            set_manager_environment(receipt["manager_environment"]["before"], run)
        elif receipt.get("manager_path"):  # Earlier receipts selected only PATH.
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
    parser.add_argument("--companions-from", action="append", default=[],
                        help="another frontend directory whose host toolchain shims are carried over")
    parser.add_argument("--manager-path", action="store_true",
                        help="select managed PATH and profile variables in the systemd user manager (Linux)")
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
               "companions": {n: str(s / n) for n, (s, _) in selection["companions"].items()},
               "manager_path": selection["manager_path"],
               "manager_environment": selection["manager_environment"], "applied": False}
    if not args.apply:
        print(json.dumps(summary, indent=1))
        return 0
    receipt_path, _ = apply(selection, run)
    print(json.dumps(dict(summary, applied=True, receipt=str(receipt_path)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
