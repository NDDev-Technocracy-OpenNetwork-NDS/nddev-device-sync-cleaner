#!/usr/bin/env python3
"""Real native cache qualification, entirely inside an owned temporary fixture."""
import argparse
import json
import os
import signal
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib

RETENTION = 259200
REPO = Path(__file__).resolve().parents[1]


def call(command, *, cwd=None, timeout=120, environment=None):
    process = subprocess.Popen([str(item) for item in command], cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
    try:
        output, error = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=10)
        raise
    if process.returncode:
        raise RuntimeError(f"native acceptance failed ({process.returncode}): {error[-3000:]}")
    return output



def qualify(binary, tools, rustc, rustdoc):
    versions = json.loads((REPO / "cache-providers.lock.json").read_text())["versions"]
    for name, expected in versions.items():
        reported = call([tools / name, "--version"]).strip()
        assert (reported.split(" (")[0] if name == "uv" else reported) == expected
    with tempfile.TemporaryDirectory(prefix="nds-cache-acceptance-") as temporary:
        base = Path(temporary).resolve()
        root = base / "cache"
        config = base / "config.json"
        rustup = shutil.which("rustup")
        if rustup:
            # Exercise the native multicall symlink layout used by macOS runners.
            native_dir = base / "native"
            native_dir.mkdir()
            shutil.copyfile(Path(rustup).resolve(), native_dir / "rustup-init")
            (native_dir / "rustup-init").chmod(0o755)
            (native_dir / "rustup").symlink_to("rustup-init")
            rustup = str(native_dir / "rustup")
        config.write_text(json.dumps({"schema": 1, "root": str(root), "cargo": str(tools / "cargo"),
                                     "rustc": str(rustc), "rustdoc": str(rustdoc), "rustup": rustup,
                                     "rustup_home": call([rustup, "show", "home"]).strip() if rustup else None,
                                     "frontends": str(base / "frontends"), "cargo_home": None,
                                     "uv": str(tools / "uv"), "bun": str(tools / "bun")}))
        call([sys.executable, REPO / "scripts/install-cache-frontends.py", config, binary])
        fixture_env = dict(os.environ)
        for key in ("CARGO", "CARGO_HOME", "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_BUILD_DIR", "RUSTC", "RUSTDOC", "RUSTUP_TOOLCHAIN", "NDS_CLEANER_MANAGED_BUILD_DIR", "NDS_CLEANER_MANAGED_TARGET_DIR", "NDS_CLEANER_NESTING"):
            fixture_env.pop(key, None)
        if rustup:
            fixture_env["RUSTUP_TOOLCHAIN"] = "1.99.1"
        def run(tool, *args, cwd=base, overrides=None):
            return call([binary, config, tool, *args], cwd=cwd, environment=dict(fixture_env, **(overrides or {})))
        run("init")
        assert run("cargo", "+1.99.1", "--version").strip() == versions["cargo"]
        packages = tomllib.loads((REPO / "Cargo.lock").read_text())["package"]
        pins = {name: next(item["version"] for item in packages if item["name"] == name)
                for name in ("itoa", "memchr")}
        dep = base / "shared-dep"
        (dep / "src").mkdir(parents=True)
        (dep / "Cargo.toml").write_text('[package]\nname="nds-cache-shared-dep"\nversion="0.0.0"\nedition="2024"\n[dependencies]\n'
                                       + ''.join(f'{name}="={version}"\n' for name, version in pins.items()))
        (dep / "src/lib.rs").write_text('pub fn value()->u64 { 7 }\n')
        apps = []
        for label in ("a", "b"):
            app = base / label
            (app / "src").mkdir(parents=True)
            (app / "Cargo.toml").write_text(f'[package]\nname="nds-cache-probe-{label}"\nversion="0.0.0"\nedition="2024"\n'
                                           '[[bin]]\nname="probe"\npath="src/main.rs"\n[dependencies]\n'
                                           'nds-cache-shared-dep={path="../shared-dep"}\n')
            (app / "src/main.rs").write_text(f'fn main(){{println!("{label} {{}}",nds_cache_shared_dep::value());}}\n')
            apps.append(app)
        def build(app):
            return [json.loads(line) for line in run("cargo", "build", "--message-format=json", cwd=app).splitlines()
                    if line.startswith('{')]
        first = build(apps[0])
        stamp = next(root.glob("cargo/lanes/*/build/debug/build/nds-cache-shared-dep/*/fingerprint/lib-nds_cache_shared_dep"))
        old = int(time.time()) - RETENTION - 10
        os.utime(stamp, (old, old))
        second = build(apps[1])
        assert next(item for item in second if item.get("target", {}).get("name") == "nds_cache_shared_dep")["fresh"]
        assert stamp.stat().st_mtime > old + RETENTION
        if rustup:
            shim = base / "frontends/rustup"
            assert call([shim, "run", "1.99.1", "cargo", "--version"], environment=fixture_env).strip() == versions["cargo"]
            assert call([shim, "which", "--toolchain", "1.99.1", "rustc"], environment=fixture_env) == call([rustup, "which", "--toolchain", "1.99.1", "rustc"], environment=fixture_env)
            events = [json.loads(line) for line in call([shim, "run", "1.99.1", "cargo", "build", "--locked", "--message-format=json"], cwd=apps[1], environment=fixture_env).splitlines() if line.startswith('{')]
            assert next(item for item in events if item.get("target", {}).get("name") == "nds_cache_shared_dep")["fresh"]
        executables = [Path(next(item["executable"] for item in events if item.get("executable")))
                       for events in (first, second)]
        assert executables[0] != executables[1]
        assert [call([path]).strip() for path in executables] == ["a 7", "b 7"]
        run("clean", "--apply")  # Observe completed foreground uses before simulating idle age.
        # Only a's unit/target is unused; b and the dependency were reused warm.
        stale = next(root.glob("cargo/lanes/*/build/debug/build/nds-cache-probe-a/*/fingerprint/bin-probe"))
        os.utime(stale, (old, old))
        target_a = executables[0].parents[2]
        (target_a / ".last-use").write_text(str(old))
        preview = json.loads(run("clean"))
        assert preview["stale_units"] >= 2 and stale.exists() and executables[0].exists()
        collected = json.loads(run("clean", "--apply"))
        assert collected["removed_units"] >= 2 and not stale.exists()
        assert stamp.exists() and executables[1].exists() and not executables[0].exists()
        # Age native Cargo's own tracking database in this fixture only.
        database = root / "cargo/home/.global-cache"
        source = root / "cargo/home/registry/src"
        itoa = next(source.glob(f'*/itoa-{pins["itoa"]}'))
        memchr = next(source.glob(f'*/memchr-{pins["memchr"]}'))
        with sqlite3.connect(database) as db:
            for table in ("registry_src", "registry_crate"):
                columns = {item[1] for item in db.execute(f'PRAGMA table_info({table})')}
                assert {"name", "timestamp"} <= columns
                db.execute(f'UPDATE {table} SET timestamp=? WHERE name LIKE ?', (old, "itoa-%"))
        run("clean")
        assert itoa.exists() and memchr.exists()
        run("clean", "--apply")
        assert not itoa.exists() and memchr.exists()
        assert next(item for item in build(apps[1]) if item.get("target", {}).get("name") == "nds_cache_shared_dep")["fresh"]
        # Native SDK callers retain their verified source home and final staging.
        provider_home=base / "provider-home"
        external_target=base / "caller-target"
        external_build=base / "caller-build"
        for path in (external_target,external_build):
            path.mkdir()
            (path / "preserve").write_text("caller-owned")
        overrides={"CARGO_HOME":str(provider_home),"CARGO_TARGET_DIR":str(external_target),
                   "CARGO_BUILD_BUILD_DIR":str(external_build),"RUSTC":str(rustc),"RUSTDOC":str(rustdoc)}
        metadata=json.loads(run("cargo","metadata","--format-version","1",cwd=apps[1],overrides=overrides))
        assert Path(metadata["target_directory"])==external_target
        assert Path(metadata["build_directory"])==external_build
        assert next(item for item in metadata["packages"] if item["name"]=="itoa")["manifest_path"].startswith(str(provider_home))
        run("cargo","build",cwd=apps[1],overrides=overrides)
        # Distinct manifests called from the same directory cannot collide.
        targets=[]
        for app in apps:
            events=[json.loads(line) for line in run("cargo","build","--manifest-path",str(app / "Cargo.toml"),"--message-format=json").splitlines() if line.startswith('{')]
            targets.append(next(item["executable"] for item in events if item.get("executable")))
        assert targets[0]!=targets[1]
        # Both native-Cargo and wrapped-Cargo build-script recursion are qualified.
        for mode in ("native","wrapped"):
            child=base / ("nested-child-"+mode)
            parent=base / ("nested-parent-"+mode)
            for path in (child,parent):
                (path / "src").mkdir(parents=True)
                (path / "Cargo.toml").write_text(f'[package]\nname="{path.name}"\nversion="0.0.0"\nedition="2024"\n')
                (path / "src/lib.rs").write_text("pub fn value()->u8 { 1 }\n")
            prefix='std::env::var_os("CARGO").unwrap()' if mode=="native" else json.dumps(str(binary))
            arguments=[] if mode=="native" else [str(config),"cargo"]
            arguments += ["build","--manifest-path",str(child / "Cargo.toml")]
            (parent / "build.rs").write_text('fn main(){let status=std::process::Command::new('+prefix+').args(['+','.join(json.dumps(arg) for arg in arguments)+']).status().unwrap();assert!(status.success());}\n')
            run("cargo","build",cwd=parent)
        run("clean","--apply")
        for path in (external_target,external_build):
            assert (path / "preserve").read_text()=="caller-owned"
        # Real Cargo child keeps the shared lease after Cargo itself is killed.
        app = apps[0]
        (app / "build.rs").write_text('fn main(){std::fs::write("lease-ready",b"ready").unwrap();'
                                      'std::thread::sleep(std::time::Duration::from_secs(8));}\n')
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen([str(binary), str(config), "cargo", "build"], cwd=app,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=fixture_env)
            try:
                deadline = time.monotonic() + 30
                while not (app / "lease-ready").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError("native child fixture did not start: " + log.read()[-2000:])
                    time.sleep(0.03)
                assert json.loads(run("clean", "--apply"))["busy"] >= 1
                process.kill()
                process.wait(timeout=5)
                orphan = json.loads(run("clean", "--apply"))
                assert orphan["busy"] >= 1 and orphan["source_gc_deferred"]
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            deadline = time.monotonic() + 12
            while json.loads(run("clean"))["busy"]:
                assert time.monotonic() < deadline
                time.sleep(0.1)
        # Native uv/Bun manage their own cached payloads. Lock/argument generations
        # retain old downloads while recently used, regardless of filesystem age.
        python = base / "python"
        python.mkdir()
        (python / "pyproject.toml").write_text('[project]\nname="nds-cache-probe"\nversion="0.0.0"\nrequires-python=">=3.15"\ndependencies=["idna"]\n')
        run("uv", "sync", "--python", "3.15.1", cwd=python)
        run("uv", "sync", "--python", "3.15.1", cwd=python)
        javascript = base / "javascript"
        javascript.mkdir()
        (javascript / "package.json").write_text(json.dumps({"private": True, "dependencies": {"is-number": "7.0.0"}}))
        run("bun", "install", cwd=javascript)
        run("bun", "install", cwd=javascript)
        shards = [path for tool in ("uv", "bun") for path in (root / tool).iterdir()]
        def count(path):
            return sum(1 for item in (path / "cache").rglob('*') if item.is_file())
        for shard in shards:
            for path in sorted((shard / "cache").rglob('*'), key=lambda p: len(p.parts), reverse=True):
                os.utime(path, (old, old), follow_symlinks=False)
            os.utime(shard / "cache", (old, old))
        before = [count(shard) for shard in shards]
        run("clean", "--apply")
        assert [count(shard) for shard in shards] == before
        for shard in shards:
            (shard / ".last-use").write_text(str(old))
        report = json.loads(run("clean", "--apply"))
        after = [count(shard) for shard in shards]
        assert report["native_cache_runs"] >= len(shards) + 1
        assert sum(after) < sum(before)
        assert call([python / ".venv/bin/python", "-c", "import idna,sys;print(sys.version_info[:3])"]).strip() == "(3, 15, 1)"
        assert call([tools / "bun", "-e", 'console.log(require("is-number")(7))'], cwd=javascript).strip() == "true"
        return {"cargo_cross_project_warm_hit": True, "rustup_cargo_routing": bool(rustup), "separate_final_binaries": True,
                "caller_owned_inputs_preserved": True, "manifest_target_isolation": True, "native_and_wrapped_nested_builds": True,
                "warm_use_refreshes_native_stamp": True, "stale_artifact_gc": True,
                "native_source_gc": True, "native_child_lease_survives_parent": True,
                "uv_bun_idle_generation_gc": True, "installed_environments_preserved": True,
                "retention_seconds": RETENTION, "versions": versions}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--rustdoc", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.binary.resolve(), args.tools.resolve(), args.rustc.resolve(), args.rustdoc.resolve())))
