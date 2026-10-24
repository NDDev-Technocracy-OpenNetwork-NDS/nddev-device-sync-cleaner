#!/usr/bin/env python3
"""Install only exact verified cache-owner binaries; no SDK or shell installer."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import stat
import tarfile
import tempfile
import urllib.request
import zipfile

LOCK = Path(__file__).resolve().parents[1] / "cache-providers.lock.json"
LIMIT = 256 * 1024 * 1024


def prepare(destination):
    lock_bytes = LOCK.read_bytes()
    lock = json.loads(lock_bytes)
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = {"x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    selected = lock["platforms"].get(f"{system}-{machine}")
    if selected is None or not destination.is_absolute():
        raise ValueError("unsupported platform or destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = {name: digest for artifact in selected.values() for name, digest in artifact["binary_sha256"].items()}
    receipt = {"schema": 1, "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
               "platform": f"{system}-{machine}", "versions": lock["versions"], "binaries": expected}
    if destination.exists():
        previous = json.loads((destination / "source.json").read_text())
        if previous["schema"] != 1 or previous["platform"] != receipt["platform"]:
            raise ValueError("existing tool installation belongs to another owner/platform")
        for name, digest in expected.items():
            path = destination / "bin" / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("existing binary differs; choose a new versioned destination")
        if previous != receipt:
            with tempfile.NamedTemporaryFile(mode="w", dir=destination, prefix=".source-", delete=False) as output:
                pending = Path(output.name)
                json.dump(receipt, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            pending.replace(destination / "source.json")
        return receipt
    with tempfile.TemporaryDirectory(prefix=".nds-cache-tools-", dir=destination.parent) as work:
        stage = Path(work) / "prepared"
        (stage / "bin").mkdir(parents=True)
        binaries = {}
        for artifact in selected.values():
            with urllib.request.urlopen(artifact["url"], timeout=60) as response:
                archive = response.read(LIMIT + 1)
            if len(archive) > LIMIT or hashlib.sha256(archive).hexdigest() != artifact["sha256"]:
                raise ValueError("archive digest or size differs from lock")
            if artifact["url"].endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive)) as compressed:
                    extracted = {}
                    for name, member in artifact["members"].items():
                        info = compressed.getinfo(member)
                        if info.file_size > LIMIT or stat.S_ISLNK(info.external_attr >> 16):
                            raise ValueError("invalid executable archive member")
                        extracted[name] = compressed.read(info)
            else:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as compressed:
                    extracted = {}
                    for name, member in artifact["members"].items():
                        info = compressed.getmember(member)
                        if not info.isfile() or info.size > LIMIT:
                            raise ValueError("invalid executable archive member")
                        extracted[name] = compressed.extractfile(info).read(LIMIT + 1)
            for name, data in extracted.items():
                if not data or len(data) > LIMIT or hashlib.sha256(data).hexdigest() != artifact["binary_sha256"][name]:
                    raise ValueError("invalid executable length")
                target = stage / "bin" / name
                with target.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(0o755)
                binaries[name] = hashlib.sha256(data).hexdigest()
        assert binaries == expected
        (stage / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if destination.exists():
            raise ValueError("destination appeared during preparation")
        stage.rename(destination)
        return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.destination)))
