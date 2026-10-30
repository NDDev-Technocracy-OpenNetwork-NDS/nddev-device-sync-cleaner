#!/usr/bin/env python3
"""Stage qualified native CLI bytes with ordinary source and SHA-256 files."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def stage(args):
    for source in (args.execution_source, args.maintenance_source):
        if not re.fullmatch(r"[0-9a-f]{40}", source):
            raise ValueError("native artifacts require full source commits")
    inputs = [args.execution, args.maintenance, args.frontend, args.tools, args.license]
    if any(path.is_symlink() or not path.is_file() for path in inputs):
        raise ValueError("native artifact inputs must be regular files")
    for path, version in ((args.execution, "alpha.5"), (args.maintenance, "alpha.6")):
        observed = subprocess.check_output([str(path.resolve()), "--version"], text=True, timeout=30)
        if observed.strip() != "nds-cleaner-cache 0.0.1-" + version:
            raise ValueError("native artifact version differs from its qualified role")
    tools = json.loads(args.tools.read_text())
    if set(tools) != {"schema", "lock_sha256", "platform", "versions", "binaries"} or tools["schema"] != 1:
        raise ValueError("native tool identity must use its existing source receipt")
    compiler = subprocess.check_output([str(args.rustc), "-vV"], text=True, timeout=30)
    if "release: 1.99.1" not in compiler.splitlines():
        raise ValueError("native compiler differs from its qualified version")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for role in ("execution", "maintenance"):
        directory = args.output / role
        directory.mkdir()
        shutil.copy2(getattr(args, role), directory / "nds-cleaner-cache")
        (directory / "SOURCE_COMMIT").write_text(getattr(args, role + "_source") + "\n")
    shutil.copy2(args.frontend, args.output / "install-cache-frontends.py")
    shutil.copy2(args.license, args.output / "LICENSE")
    shutil.copy2(args.tools, args.output / "cache-tools-source.json")
    (args.output / "rustc-vV.txt").write_text(compiler)
    entries = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file():
            entries.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(args.output).as_posix())
    (args.output / "SHA256SUMS").write_text("\n".join(entries) + "\n")
    print("Staged qualified execution/maintenance CLI artifacts and SHA256SUMS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("execution", "maintenance", "frontend", "tools", "license", "rustc", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execution-source", required=True)
    parser.add_argument("--maintenance-source", required=True)
    stage(parser.parse_args())
