#!/usr/bin/env python3
"""Install local argv-preserving cache frontends into an explicit empty prefix."""
import argparse
import json
from pathlib import Path
import shlex
import tempfile

MARKER = "# NDS managed development cache frontend\n"


def install(config_path, binary):
    config_path = config_path.resolve(strict=True)
    binary = binary.resolve(strict=True)
    config = json.loads(config_path.read_text())
    root = Path(config["frontends"])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("frontends require an absolute owned prefix")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = list(root.iterdir())
    if existing and not (root / ".nds-cache-frontends").is_file():
        raise ValueError("refuse to adopt an unrelated frontend directory")
    commands = {"cargo": ["cargo"], "uv": ["uv"], "uvx": ["uv", "tool", "run"],
                "bun": ["bun"], "bunx": ["bun", "x"]}
    contents = {name: "#!/bin/sh\n" + MARKER + "exec " + shlex.join([str(binary), str(config_path), *operation]) + ' "$@"\n'
                for name, operation in commands.items()}
    if config.get("rustup"):
        native = Path(config["rustup"])
        # Rustup is a multicall binary: preserve its invoked name through symlinks.
        if not native.is_absolute() or not native.is_file() or native.resolve() == (root / "rustup").resolve():
            raise ValueError("rustup backend must be a separate native executable")
        contents["rustup"] = ("#!/bin/sh\n" + MARKER
            + 'if [ "$#" -ge 3 ] && [ "$1" = run ] && [ "$3" = cargo ]; then\n'
            + '  nds_cache_channel=$2\n  shift 3\n  exec '
            + shlex.join([str(binary), str(config_path), "cargo"])
            + ' "+$nds_cache_channel" "$@"\nfi\nexec ' + shlex.quote(str(native)) + ' "$@"\n')
    for name, content in contents.items():
        target = root / name
        if target.exists() and (target.is_symlink() or not target.read_text().startswith("#!/bin/sh\n" + MARKER)):
            raise ValueError("refuse to replace an unrelated frontend")
        if target.exists() and target.read_text() == content:
            continue
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".frontend-", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(content)
        pending.chmod(0o755)
        pending.replace(target)
    (root / ".nds-cache-frontends").write_text("1\n")
    return {"installed": sorted(contents)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("binary", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(install(arguments.config, arguments.binary)))
