#!/usr/bin/env python3
"""Real native cache qualification, entirely inside an owned temporary fixture."""
import argparse
import hashlib
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



def qualify_caller_targets(run, base, root, *, binary, config, tools, rustc, rustdoc, environment):
    """Exercise caller placement with the real frontend, Cargo and compiler."""
    home = base / "caller source home"
    home.mkdir(mode=0o700)
    sentinel = home / "owner-input"
    sentinel.write_bytes(b"preserved caller source home")
    lock = b'version = 3\n[[package]]\nname = "nds-caller-target"\nversion = "0.0.0"\n'

    def project(path):
        (path / "src").mkdir(parents=True)
        (path / "Cargo.toml").write_text(
            '[package]\nname="nds-caller-target"\nversion="0.0.0"\nedition="2024"\n'
            '[[bin]]\nname="caller-placement"\npath="src/main.rs"\n'
        )
        (path / "Cargo.lock").write_bytes(lock)
        (path / "input.txt").write_text("current checkout")
        (path / "src/lib.rs").write_text('''#[test]
fn reads_current_checkout() {
    let compiled = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    assert_eq!(compiled, std::env::current_dir().unwrap());
    assert_eq!(std::fs::read_to_string(compiled.join("input.txt")).unwrap(), "current checkout");
}
''')
        (path / "src/main.rs").write_text('''fn main() {
    assert_eq!(std::env::args().skip(1).collect::<Vec<_>>(), ["--target-dir", "literal"]);
    println!("argv preserved");
}
''')

    def artifact(output, name):
        return next(item for item in (json.loads(line) for line in output.splitlines() if line.startswith('{'))
                    if item.get("reason") == "compiler-artifact" and item["target"]["name"] == name
                    and item.get("executable"))

    def ledgers():
        return {str(path): path.read_bytes() for path in root.glob("cargo/lanes/*/targets/*/.last-use")}

    def owned_directory(path):
        path.mkdir(parents=True)
        marker = path / "preserve"
        marker.write_bytes(b"caller-owned placement")
        return marker

    proofs = []
    sentinels = [sentinel]
    modes = ("env", "alias", "cli", "equals", "cli-priority", "env-priority",
             "independent-build", "cli-independent-build")
    for mode in modes:
        case = base / ("caller-" + mode)
        first = case / "first checkout"
        second = case / "second checkout"
        project(first)
        targets = [case / name for name in ("first target", "second target")]
        builds = [case / name for name in ("first build", "second build")]
        ignored = [case / name for name in ("ignored special", "ignored alias")]
        sentinels.extend(owned_directory(path) for path in targets + ignored)
        independent_build = mode in ("independent-build", "cli-independent-build")
        if independent_build:
            sentinels.extend(owned_directory(path) for path in builds)
        before = ledgers()
        fresh = []
        for index, checkout in ((0, first), (1, second), (1, second)):
            if index == 1 and first.exists():
                shutil.copytree(first, second, copy_function=shutil.copy2)
                assert (first / "src/lib.rs").stat().st_mtime_ns == (second / "src/lib.rs").stat().st_mtime_ns
                shutil.rmtree(first)  # Only this fresh, owned fixture checkout.
            target = targets[index]
            build = builds[index] if independent_build else target
            overrides = {"CARGO_HOME": str(home)}
            options = []
            if mode in ("env", "env-priority", "independent-build"):
                overrides["CARGO_TARGET_DIR"] = str(target)
            elif mode == "alias":
                overrides["CARGO_BUILD_TARGET_DIR"] = str(target)
            else:
                options = ["--target-dir=" + str(target)] if mode == "equals" else ["--target-dir", str(target)]
            if mode in ("cli-priority", "env-priority"):
                overrides["CARGO_BUILD_TARGET_DIR"] = str(ignored[1])
            if mode == "cli-priority":
                overrides["CARGO_TARGET_DIR"] = str(ignored[0])
            if independent_build:
                overrides["CARGO_BUILD_BUILD_DIR"] = str(build)
            # Native metadata has no dedicated --target-dir option. CLI cases
            # instead prove intermediate and final paths from actual artifacts.
            if not options:
                metadata = json.loads(run("cargo", "metadata", "--locked", "--offline", "--no-deps",
                                          "--format-version", "1", cwd=checkout, overrides=overrides))
                assert Path(metadata["target_directory"]) == target
                assert Path(metadata["build_directory"]) == build
            output = run("cargo", "test", "--locked", "--offline", "--lib", "--message-format=json",
                         *options, cwd=checkout, overrides=overrides)
            test = artifact(output, "nds_caller_target")
            assert Path(test["executable"]).is_relative_to(build)
            fresh.append(test["fresh"])
        assert fresh == [False, False, True], (mode, fresh)
        final = artifact(run("cargo", "build", "--locked", "--offline", "--bin", "caller-placement",
                             "--message-format=json", *options, cwd=second, overrides=overrides), "caller-placement")
        assert Path(final["executable"]).parent == targets[1] / "debug"
        assert (second / "Cargo.lock").read_bytes() == lock
        assert ledgers() == before  # No fictional managed final-output use.
        assert all(sorted(path.iterdir()) == [path / "preserve"] for path in ignored)
        proofs.append({"mode": mode, "fresh": fresh, "target": str(targets[1]),
                       "test_executable": test["executable"], "final_executable": final["executable"]})

    # New explicit child targets override inherited managed defaults, including
    # an inherited build env equal to the parent's managed build hint.
    nested_modes = ("env", "alias", "cli", "equals", "independent-build")
    for mode in nested_modes:
        case = base / ("caller-nested-" + mode)
        parent = case / "parent"
        child = case / "child"
        target = case / "child target"
        build = case / "child build" if mode == "independent-build" else target
        project(parent)
        project(child)
        parent_name = "nds-caller-parent-" + mode
        (parent / "Cargo.toml").write_text((parent / "Cargo.toml").read_text().replace("nds-caller-target", parent_name))
        parent_lock = lock.replace(b"nds-caller-target", parent_name.encode())
        (parent / "Cargo.lock").write_bytes(parent_lock)
        sentinels.append(owned_directory(target))
        if build != target:
            sentinels.append(owned_directory(build))
        proof = case / "child-artifacts.jsonl"
        arguments = [str(config), "cargo", "test", "--locked", "--offline", "--lib", "--message-format=json",
                     "--manifest-path", str(child / "Cargo.toml")]
        if mode == "cli":
            arguments += ["--target-dir", str(target)]
        elif mode == "equals":
            arguments += ["--target-dir=" + str(target)]
        variable = "CARGO_TARGET_DIR" if mode in ("env", "independent-build") else "CARGO_BUILD_TARGET_DIR"
        placement = (f'.env({json.dumps(variable)},{json.dumps(str(target))})'
                     if mode in ("env", "alias", "independent-build") else "")
        build_choice = (json.dumps(str(build)) if mode == "independent-build"
                        else 'std::env::var_os("NDS_CLEANER_MANAGED_BUILD_DIR").unwrap()')
        (parent / "build.rs").write_text(
            'fn main(){let output=std::process::Command::new(' + json.dumps(str(binary)) + ').args(['
            + ','.join(json.dumps(arg) for arg in arguments) + ']).current_dir(' + json.dumps(str(child)) + ')'
            + placement + '.env("CARGO_HOME",' + json.dumps(str(home)) + ')'
            + '.env("CARGO_BUILD_BUILD_DIR",' + build_choice + ')'
            + '.output().unwrap();assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));'
            + 'std::fs::write(' + json.dumps(str(proof)) + ',output.stdout).unwrap();}\n'
        )
        run("cargo", "build", "--locked", "--offline", cwd=parent, overrides={"CARGO_HOME": str(home)})
        test = artifact(proof.read_text(), "nds_caller_target")
        assert not test["fresh"] and Path(test["executable"]).is_relative_to(build)
        assert (parent / "Cargo.lock").read_bytes() == parent_lock
        assert (child / "Cargo.lock").read_bytes() == lock

    # Benign parent config must not erase generated recursion context. Child
    # metadata observes actual native paths without compiling the child.
    nesting_context = []
    for label, options in (("plain", []), ("offline", ["--config", "net.offline=true"])):
        case = base / ("managed-context-" + label)
        parent = case / "parent"
        child = case / "child"
        project(parent)
        project(child)
        parent_name = "nds-context-parent-" + label
        (parent / "Cargo.toml").write_text((parent / "Cargo.toml").read_text().replace("nds-caller-target", parent_name))
        parent_lock = lock.replace(b"nds-caller-target", parent_name.encode())
        (parent / "Cargo.lock").write_bytes(parent_lock)
        proof = case / "child-metadata.json"
        context = [(name, case / filename) for name, filename in (
            ("NDS_CLEANER_MANAGED_BUILD_DIR", "parent-build"),
            ("NDS_CLEANER_MANAGED_TARGET_DIR", "parent-target"),
            ("NDS_CLEANER_NESTING", "parent-depth"), ("OUT_DIR", "parent-out"))]
        arguments = [str(config), "cargo", "metadata", "--locked", "--offline", "--no-deps",
                     "--format-version", "1", "--manifest-path", str(child / "Cargo.toml")]
        (parent / "build.rs").write_text(
            'fn main(){for (name,path) in ['
            + ','.join('(' + json.dumps(name) + ',' + json.dumps(str(path)) + ')' for name, path in context)
            + ']{std::fs::write(path,std::env::var_os(name).expect("managed context missing").as_encoded_bytes()).unwrap();}'
            + 'let output=std::process::Command::new(' + json.dumps(str(binary)) + ').args(['
            + ','.join(json.dumps(arg) for arg in arguments) + ']).current_dir(' + json.dumps(str(child)) + ')'
            + '.output().unwrap();assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));'
            + 'std::fs::write(' + json.dumps(str(proof)) + ',output.stdout).unwrap();}\n'
        )
        run("cargo", "build", "--locked", "--offline", *options, cwd=parent,
            overrides={"CARGO_HOME": str(home)})
        parent_build = Path((case / "parent-build").read_text())
        parent_target = Path((case / "parent-target").read_text())
        assert (case / "parent-depth").read_text() == "0"
        assert Path((case / "parent-out").read_text()).is_relative_to(parent_build)
        parent_parts = parent_build.relative_to(root / "cargo/lanes").parts
        assert len(parent_parts) == 2 and parent_parts[1] == "build"
        assert parent_target.relative_to(root / "cargo/lanes").parts[1] == "targets"
        # Context is deliberately distinct from effective-path retention proof.
        assert (parent_target.parent / ".last-use").exists() == (not options)
        metadata = json.loads(proof.read_text())
        paths = []
        for depth in range(1, 9):
            if depth > 1:
                metadata = json.loads(run("cargo", "metadata", "--locked", "--offline", "--no-deps",
                                          "--format-version", "1", *options, cwd=child, overrides=inherited))
            build_parts = Path(metadata["build_directory"]).relative_to(root / "cargo/lanes").parts
            target_parts = Path(metadata["target_directory"]).relative_to(root / "cargo/lanes").parts
            assert len(build_parts) == 5 and build_parts[1:4] == ("build", "nested", str(depth))
            assert len(target_parts) == 4 and target_parts[1] == "targets" and target_parts[3] == "cache"
            assert build_parts[0] == target_parts[0] == parent_parts[0] and build_parts[4] == target_parts[2]
            paths.append(metadata["build_directory"])
            # Repeat actual observed native placement as inherited protocol
            # context; this is not a claim of eight recursive compiler builds.
            inherited = {"CARGO_HOME": str(home), "NDS_CLEANER_MANAGED_BUILD_DIR": metadata["build_directory"],
                         "NDS_CLEANER_MANAGED_TARGET_DIR": metadata["target_directory"], "NDS_CLEANER_NESTING": str(depth)}
        before = ledgers()
        try:
            run("cargo", "metadata", "--locked", "--offline", "--no-deps", "--format-version", "1",
                *options, cwd=child, overrides=inherited)
        except RuntimeError as error:
            assert "NDS Cleaner: managed cache configuration is invalid" in str(error)
        else:
            raise AssertionError("inherited nesting depth9 unexpectedly succeeded")
        assert ledgers() == before
        assert (parent / "Cargo.lock").read_bytes() == parent_lock and (child / "Cargo.lock").read_bytes() == lock
        nesting_context.append({"parent_config": label, "parent_depth": 0, "parent_hints_present": True,
                                "child_build_directories": paths, "inherited_depth9_refused": True})

    # A caller may select an actual known managed target in another lane, also
    # as its explicit build-dir: retention follows namespace, not argument role.
    lane = hashlib.sha256(b"owned caller-target acceptance lane").hexdigest()
    workspace = hashlib.sha256(b"owned caller-target acceptance workspace").hexdigest()
    managed = root / "cargo/lanes" / lane / "targets" / workspace / "cache"
    metadata = json.loads(run("cargo", "metadata", "--locked", "--offline", "--no-deps", "--format-version", "1",
                              cwd=second, overrides={"CARGO_HOME": str(home), "CARGO_TARGET_DIR": str(managed),
                                                     "CARGO_BUILD_BUILD_DIR": str(managed)}))
    assert Path(metadata["target_directory"]) == Path(metadata["build_directory"]) == managed
    assert int((managed.parent / ".last-use").read_text()) > 0
    assert (managed.parent / ".pending-use").read_bytes() == b"1"
    assert (managed.parents[2] / ".lease").is_file()
    lease_project = base / "caller-lease"
    project(lease_project)
    lease_probe = '''import fcntl, sys
with open(sys.argv[1], "r+") as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError("actual selected lane lease was not inherited")
'''
    (lease_project / "build.rs").write_text(
        'fn main(){assert!(std::process::Command::new(' + json.dumps(sys.executable) + ').args(['
        + ','.join(json.dumps(arg) for arg in ("-c", lease_probe, str(managed.parents[2] / ".lease")))
        + ']).status().unwrap().success());}\n'
    )
    run("cargo", "build", "--locked", "--offline", "--target-dir", str(managed), cwd=lease_project,
        overrides={"CARGO_HOME": str(home), "CARGO_BUILD_BUILD_DIR": str(managed)})
    assert (lease_project / "Cargo.lock").read_bytes() == lock

    forwarded = run("cargo", "run", "--locked", "--offline", "--bin", "caller-placement",
                    "--message-format=json", "--", "--target-dir", "literal", cwd=second,
                    overrides={"CARGO_HOME": str(home)})
    assert "argv preserved" in forwarded.splitlines()
    executable = Path(artifact(forwarded, "caller-placement")["executable"])
    assert executable.is_relative_to(root / "cargo/lanes") and "targets" in executable.parts

    # Compare actual native errors rather than replacing malformed argv with a
    # frontend-specific configuration refusal. These commands cannot compile.
    control_env = dict(environment, CARGO_HOME=str(home), RUSTC=str(rustc), RUSTDOC=str(rustdoc))
    for options in (("--target-dir",), ("--target-dir", str(base / "invalid target"), "--nds-invalid-target-option")):
        errors = []
        for command in ([tools / "cargo", "test", "--locked", "--offline", *options],
                        [binary, config, "cargo", "test", "--locked", "--offline", *options]):
            try:
                call(command, cwd=second, environment=control_env)
            except RuntimeError as error:
                errors.append(str(error))
            else:
                raise AssertionError("invalid native target argument unexpectedly succeeded")
        assert errors[0] == errors[1] and "NDS Cleaner:" not in errors[1]
    assert sentinel.read_bytes() == b"preserved caller source home"
    assert all(path.read_bytes() == b"caller-owned placement" for path in sentinels[1:])
    return {"relocation": proofs, "nested_explicit_target_modes": list(nested_modes),
            "managed_nesting_context": nesting_context,
            "known_managed_target_and_build_namespace": True, "native_errors_and_forwarded_argv": True,
            "caller_home_sentinel_sha256": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
            "config_parity": "not qualified: hierarchical config and later CLI config remain separate gaps"}


def qualify(binary, tools, rustc, rustdoc, *, caller_targets_only=False):
    versions = json.loads((REPO / "cache-providers.lock.json").read_text())["versions"]
    for name, expected in ({"cargo": versions["cargo"]} if caller_targets_only else versions).items():
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
        if caller_targets_only:
            return qualify_caller_targets(run, base, root, binary=binary, config=config, tools=tools,
                                          rustc=rustc, rustdoc=rustdoc, environment=fixture_env)
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
        caller_targets = qualify_caller_targets(run, base, root, binary=binary, config=config, tools=tools,
                                               rustc=rustc, rustdoc=rustdoc, environment=fixture_env)
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
                "caller_owned_inputs_preserved": True, "caller_targets": caller_targets,
                "manifest_target_isolation": True, "native_and_wrapped_nested_builds": True,
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
    parser.add_argument("--caller-targets-only", action="store_true",
                        help="run only real offline Cargo caller-target qualification in the owned fixture")
    args = parser.parse_args()
    print(json.dumps(qualify(args.binary.resolve(), args.tools.resolve(), args.rustc.resolve(), args.rustdoc.resolve(),
                             caller_targets_only=args.caller_targets_only)))
