#!/usr/bin/env python3
"""Install only exact verified cache-owner binaries; no SDK or shell installer."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import tarfile
import tempfile
import urllib.request
import urllib.parse
import zipfile

LOCK = Path(__file__).resolve().parents[1] / "cache-providers.lock.json"
LIMIT = 256 * 1024 * 1024



def central_base(value):
    if value is None:
        return None
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("cache base must be a literal HTTP IPv4 loopback listener with a port")
    return value.rstrip("/")


def download_url(url, digest, base):
    if base is None:
        return url
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment):
        raise ValueError("tool source is not an admitted public artifact URL")
    if parsed.netloc == "static.rust-lang.org":
        return base + "/rustup" + parsed.path
    if parsed.netloc == "github.com" and re.fullmatch(r"/[^/]+/[^/]+/releases/download/[^/]+/[^/]+", parsed.path):
        return base + "/github-releases/sha256/" + digest + parsed.path
    raise ValueError("tool source has no admitted central cache route")


class CentralNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prepare(destination, cache_base=None):
    cache_base = central_base(cache_base)
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
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), CentralNoRedirect()) if cache_base else None
        for artifact in selected.values():
            url = download_url(artifact["url"], artifact["sha256"], cache_base)
            open_url = opener.open if opener else urllib.request.urlopen
            with open_url(url, timeout=60) as response:
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
    parser.add_argument("--cache-base", help="selected literal loopback network cache; failure never falls back upstream")
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.destination, arguments.cache_base)))
