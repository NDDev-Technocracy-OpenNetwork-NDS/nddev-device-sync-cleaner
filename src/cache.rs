//! Opt-in local development caches. This is not an agent/remote mutation port.
//! Cargo owns source GC; uv and Bun own deletion inside their idle cache shards.
use cap_std::{
    ambient_authority,
    fs::{Dir, OpenOptions},
};
use nddev_device_sync_adapter_io::{CancellationToken, NativeIo, ProcessRequest};
use rustix::{fs::FlockOperation, io::FdFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs,
    io::{self, Read, Write},
    os::unix::{
        ffi::OsStringExt,
        fs::{DirBuilderExt, MetadataExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(target_os = "linux")]
mod bun_guard;

const BUN_LIFETIME: &[u8] = b"NDS Bun descendant guard v1\n";
const ENVIRONMENTS: &[u8] = b"NDS installed project environments v1\n";

// An inherited fd alone is insufficient: Bun closes it before launching some
// descendants. Old generations and interrupted guards remain unqualified.
fn bun_completed(dir: &Dir) -> Result<bool> {
    if regular(dir, Path::new(".bun-lifetime"), BUN_LIFETIME.len())? != BUN_LIFETIME {
        return Ok(false);
    }
    Ok(directory(dir, Path::new(".bun-runs"))?
        .entries()?
        .next()
        .is_none())
}

pub const RETENTION: Duration = Duration::from_secs(3 * 24 * 60 * 60);
const MARKER: &[u8] = b"NDS managed rebuildable cache v1\n";
fn qualified(name: &str) -> Result<String> {
    let lock: serde_json::Value =
        serde_json::from_str(include_str!("../cache-providers.lock.json"))
            .map_err(|_| CacheError::Configuration)?;
    let os = if cfg!(target_os = "macos") {
        "darwin"
    } else {
        std::env::consts::OS
    };
    let arch = if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        std::env::consts::ARCH
    };
    let platform = format!("{os}-{arch}");
    if !lock["platforms"][&platform].is_object() {
        return Err(CacheError::Provider);
    }
    lock["platforms"][&platform][name]["version_report"]
        .as_str()
        .or_else(|| lock["versions"][name].as_str())
        .map(str::to_owned)
        .ok_or(CacheError::Configuration)
}
const MAX_ENTRIES: usize = 100_000;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema: u8,
    pub root: PathBuf,
    pub cargo: PathBuf,
    pub rustc: PathBuf,
    pub rustdoc: PathBuf,
    pub rustup: Option<PathBuf>,
    pub rustup_home: Option<PathBuf>,
    pub frontends: Option<PathBuf>,
    pub cargo_home: Option<PathBuf>,
    pub uv: Option<PathBuf>,
    pub bun: Option<PathBuf>,
    pub scan_entries: Option<usize>,
    pub retention_seconds: Option<u64>,
    pub environment_root: Option<PathBuf>,
}

impl Config {
    fn retention(&self) -> Duration {
        Duration::from_secs(self.retention_seconds.unwrap_or(RETENTION.as_secs()))
    }
}

#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    #[error("managed cache configuration is invalid")]
    Configuration,
    #[error("cache is not owned, private or initialized")]
    Ownership,
    #[error("cache operation is busy; active work is preserved")]
    Busy,
    #[error("cache lease is not initialized")]
    Uninitialized,
    #[error("cache traversal budget is exhausted")]
    Budget,
    #[error("cache provider is unavailable or differs from its qualified version")]
    Provider,
    #[error("{provider} {operation} failed: {reason} (exit {exit_code:?})")]
    Native {
        provider: &'static str,
        operation: &'static str,
        reason: &'static str,
        exit_code: Option<i32>,
    },
    #[error("cache filesystem operation failed")]
    Io,
}
impl From<io::Error> for CacheError {
    fn from(_: io::Error) -> Self {
        Self::Io
    }
}
impl From<rustix::io::Errno> for CacheError {
    fn from(_: rustix::io::Errno) -> Self {
        Self::Io
    }
}
type Result<T> = std::result::Result<T, CacheError>;

pub fn read_config(path: &Path) -> Result<Config> {
    let file = fs::File::open(path)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > 16_384 {
        return Err(CacheError::Configuration);
    }
    let mut bytes = Vec::new();
    file.take(16_385).read_to_end(&mut bytes)?;
    let config: Config = serde_json::from_slice(&bytes).map_err(|_| CacheError::Configuration)?;
    if config.schema != 1
        || config.retention_seconds.is_some_and(|seconds| {
            !(86_400..=365 * 86_400).contains(&seconds) || seconds % 86_400 != 0
        })
        || config
            .scan_entries
            .is_some_and(|limit| limit == 0 || limit > MAX_ENTRIES)
        || !config.root.is_absolute()
        || config.root.parent().is_none()
        || [&config.cargo, &config.rustc, &config.rustdoc]
            .into_iter()
            .chain(config.rustup.iter())
            .chain(config.rustup_home.iter())
            .chain(config.frontends.iter())
            .chain(config.cargo_home.iter())
            .chain(config.uv.iter())
            .chain(config.bun.iter())
            .chain(config.environment_root.iter())
            .any(|p| !p.is_absolute())
    {
        return Err(CacheError::Configuration);
    }
    Ok(config)
}

pub struct ManagedCache {
    config: Config,
    root: Dir,
}

fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
fn now() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .map_err(|_| CacheError::Configuration)
}
fn expired(last: u64, current: u64, retention: Duration) -> bool {
    current
        .checked_sub(last)
        .is_some_and(|age| age > retention.as_secs())
}
fn hash_name(name: &str, length: usize) -> bool {
    name.len() == length
        && name
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn executable(value: OsString, cwd: &Path) -> Result<PathBuf> {
    let path = PathBuf::from(value);
    if path.is_absolute() {
        return Ok(path);
    }
    if path.components().count() > 1 {
        return Ok(cwd.join(path));
    }
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|dir| dir.join(&path))
        .find(|path| path.is_file())
        .ok_or(CacheError::Provider)
}
fn directory(dir: &Dir, path: &Path) -> Result<Dir> {
    let metadata = dir.symlink_metadata(path)?;
    if !metadata.is_dir() || metadata.is_symlink() {
        return Err(CacheError::Ownership);
    }
    Ok(dir.open_dir(path)?)
}
fn regular(dir: &Dir, path: &Path, limit: usize) -> Result<Vec<u8>> {
    let meta = dir.symlink_metadata(path)?;
    if !meta.is_file() || meta.is_symlink() || meta.len() > limit as u64 {
        return Err(CacheError::Ownership);
    }
    let mut bytes = Vec::new();
    dir.open(path)?
        .take((limit + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(CacheError::Ownership);
    }
    Ok(bytes)
}
fn lease(dir: &Dir, exclusive: bool, inherit: bool) -> Result<fs::File> {
    let metadata = match dir.symlink_metadata(".lease") {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Err(CacheError::Uninitialized);
        }
        result => result?,
    };
    if !metadata.is_file() || metadata.is_symlink() {
        return Err(CacheError::Ownership);
    }
    let file = dir
        .open_with(".lease", OpenOptions::new().read(true).write(true))?
        .into_std();
    rustix::fs::flock(
        &file,
        if exclusive {
            FlockOperation::NonBlockingLockExclusive
        } else {
            FlockOperation::LockShared
        },
    )
    .map_err(|error| {
        if error == rustix::io::Errno::WOULDBLOCK {
            CacheError::Busy
        } else {
            CacheError::Io
        }
    })?;
    if inherit {
        rustix::io::fcntl_setfd(&file, FdFlags::empty())?;
    }
    Ok(file)
}
fn initialize_directory(dir: &Dir) -> Result<()> {
    match dir.open_with(".lease", OpenOptions::new().write(true).create_new(true)) {
        Ok(file) => {
            file.set_permissions(cap_std::fs::Permissions::from_std(
                fs::Permissions::from_mode(0o600),
            ))?;
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}
// Probe the native owner's actual lock, including writers which predate the
// frontend. Never retain these locks while spawning the native GC itself.
fn native_locks_available(dir: &Dir, names: &[&str]) -> Result<bool> {
    let mut guards = Vec::new();
    for name in names {
        let metadata = match dir.symlink_metadata(name) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            result => result?,
        };
        if !metadata.is_file() || metadata.is_symlink() {
            return Err(CacheError::Ownership);
        }
        let file = dir
            .open_with(name, OpenOptions::new().read(true).write(true))?
            .into_std();
        match rustix::fs::flock(&file, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => guards.push(file),
            Err(rustix::io::Errno::WOULDBLOCK) => return Ok(false),
            Err(error) => return Err(error.into()),
        }
    }
    Ok(true)
}

fn observe_native_use(dir: &Dir, current: u64, apply: bool) -> Result<()> {
    if !apply {
        return Ok(());
    }
    match dir.symlink_metadata(".pending-use") {
        Ok(_) => {
            if regular(dir, Path::new(".pending-use"), 1)? != b"1" {
                return Err(CacheError::Ownership);
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            // An observed native writer is real use. Completion is acknowledged
            // later, after both the frontend and the native owner are idle.
            record_use(dir, current.max(last_use(dir)?))?;
        }
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn use_cache(dir: &Dir) -> Result<()> {
    let current = now()?;
    record_use(dir, current)
}
fn record_use(dir: &Dir, current: u64) -> Result<()> {
    let temporary = format!(
        ".use-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| CacheError::Configuration)?
            .as_nanos()
    );
    let mut file = dir.open_with(&temporary, OpenOptions::new().write(true).create_new(true))?;
    file.write_all(current.to_string().as_bytes())?;
    file.sync_all()?;
    dir.rename(&temporary, dir, ".last-use")?;
    dir.write(".pending-use", b"1")?;
    Ok(())
}
// The native owner replaces the wrapper and may run for days. A completed-use
// marker is acknowledged only after an exclusive lease proves it has finished.
// This conservatively starts idle-shard age at the first maintenance observation.
fn completed_use(dir: &Dir, current: u64, apply: bool) -> Result<bool> {
    match dir.symlink_metadata(".pending-use") {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
        Ok(_) => {
            if regular(dir, Path::new(".pending-use"), 1)? != b"1" {
                return Err(CacheError::Ownership);
            }
            if apply {
                let temporary = format!(
                    ".completed-{}-{}",
                    std::process::id(),
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .map_err(|_| CacheError::Configuration)?
                        .as_nanos()
                );
                let mut file =
                    dir.open_with(&temporary, OpenOptions::new().write(true).create_new(true))?;
                file.write_all(current.max(last_use(dir)?).to_string().as_bytes())?;
                file.sync_all()?;
                dir.rename(&temporary, dir, ".last-use")?;
                dir.remove_file(".pending-use")?;
            }
            Ok(true)
        }
    }
}
fn last_use(dir: &Dir) -> Result<u64> {
    String::from_utf8(regular(dir, Path::new(".last-use"), 32)?)
        .map_err(|_| CacheError::Ownership)?
        .parse()
        .map_err(|_| CacheError::Ownership)
}
fn prepare(dir: &Dir, path: &Path) -> Result<Dir> {
    dir.create_dir_all(path)?;
    let child = directory(dir, path)?;
    initialize_directory(&child)?;
    Ok(child)
}

fn fallback_toolchain(cwd: &Path, release: &str, host: &str) -> Result<()> {
    for parent in cwd.ancestors() {
        for (name, is_toml) in [("rust-toolchain.toml", true), ("rust-toolchain", false)] {
            let path = parent.join(name);
            if !path.exists() {
                continue;
            }
            let file = fs::File::open(path)?;
            if !file.metadata()?.is_file() || file.metadata()?.len() > 16_384 {
                return Err(CacheError::Configuration);
            }
            let mut value = String::new();
            file.take(16_385).read_to_string(&mut value)?;
            let channel = if is_toml {
                let parsed: toml::Value =
                    toml::from_str(&value).map_err(|_| CacheError::Configuration)?;
                parsed
                    .get("toolchain")
                    .and_then(|v| v.get("channel"))
                    .and_then(toml::Value::as_str)
                    .map(str::to_owned)
                    .ok_or(CacheError::Configuration)?
            } else {
                value.trim().to_owned()
            };
            if channel == release
                || channel == format!("{release}-{host}")
                || (channel == "stable" && !release.contains('-'))
            {
                return Ok(());
            }
            return Err(CacheError::Configuration);
        }
    }
    Ok(())
}

fn tool_directory(tool: &str, cwd: &Path, args: &[OsString]) -> Result<PathBuf> {
    let mut working = if tool == "uv" {
        std::env::var_os("UV_WORKING_DIR").map(PathBuf::from)
    } else {
        None
    };
    let mut project = if tool == "uv" {
        std::env::var_os("UV_PROJECT").map(PathBuf::from)
    } else {
        None
    };
    let mut arguments = args.iter().take_while(|arg| arg.as_os_str() != "--");
    while let Some(argument) = arguments.next() {
        let options: &[&str] = if tool == "uv" {
            &["--directory", "--project"]
        } else {
            &["--cwd"]
        };
        for &option in options {
            let destination = if option == "--project" {
                &mut project
            } else {
                &mut working
            };
            if argument == option {
                *destination = Some(PathBuf::from(
                    arguments.next().ok_or(CacheError::Configuration)?,
                ));
                break;
            }
            let prefix = format!("{option}=");
            if let Some(value) = argument.as_encoded_bytes().strip_prefix(prefix.as_bytes()) {
                *destination = Some(PathBuf::from(OsString::from_vec(value.to_vec())));
                break;
            }
        }
    }
    let working = fs::canonicalize(cwd.join(working.unwrap_or_default()))?;
    Ok(fs::canonicalize(working.join(project.unwrap_or_default()))?)
}

fn tool_generation(tool: &str, cwd: &Path, args: &[OsString], current: u64) -> Result<String> {
    let selected = tool_directory(tool, cwd, args)?;
    let lock = if tool == "uv" { "uv.lock" } else { "bun.lock" };
    let mut workspace = selected.clone();
    let mut locked = None;
    for parent in selected.ancestors().take(64) {
        let path = parent.join(lock);
        // Source lockfiles are read-only caller inputs; native projects may
        // legitimately symlink them. Cache control files still reject links.
        match fs::metadata(&path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
            Ok(meta) if !meta.is_file() => {
                return Err(CacheError::Configuration);
            }
            Ok(_) => {}
        }
        let mut content = Vec::new();
        fs::File::open(path)?
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut content)?;
        if content.len() > 16 * 1024 * 1024 {
            return Err(CacheError::Configuration);
        }
        workspace = parent.to_owned();
        locked = Some(content);
        break;
    }
    // v2 is a separate namespace: never copy/adopt a v1 payload or alter its
    // lease or last-use markers. Native providers own package/ABI/index keys;
    // presentation, offline mode and command argv are not compatibility keys.
    let mut identity = if tool == "bun" {
        b"NDS native Bun descendant cache generation v3\0".to_vec()
    } else {
        b"NDS native tool cache generation v2\0".to_vec()
    };
    for part in [
        tool.as_bytes(),
        qualified(tool)?.as_bytes(),
        std::env::consts::OS.as_bytes(),
        std::env::consts::ARCH.as_bytes(),
        workspace.as_os_str().as_encoded_bytes(),
    ] {
        identity.extend_from_slice(&(part.len() as u64).to_le_bytes());
        identity.extend_from_slice(part);
    }
    match locked {
        Some(content) => {
            identity.push(1);
            identity.extend(content);
        }
        None => {
            identity.push(0);
            identity.extend_from_slice(&(current / 86400).to_le_bytes());
        }
    }
    Ok(digest(&identity))
}

fn external_command(args: &[OsString]) -> Option<(usize, &str)> {
    let mut index = 0;
    while let Some(argument) = args.get(index)?.to_str() {
        match argument {
            "clippy" | "nextest" => return Some((index, argument)),
            "--config" | "--color" | "-Z" => {
                args.get(index + 1)?;
                index += 2;
            }
            "--offline" | "--locked" | "--frozen" | "--quiet" | "--verbose" => index += 1,
            value
                if value.starts_with("--config=")
                    || value.starts_with("--color=")
                    || (value.starts_with("-Z") && value.len() > 2)
                    || (value.starts_with('-')
                        && value.len() > 1
                        && value[1..].bytes().all(|b| matches!(b, b'v' | b'q'))) =>
            {
                index += 1
            }
            _ => return None,
        }
    }
    None
}

impl ManagedCache {
    fn environments(config: &Config, initialize: bool) -> Result<Option<Dir>> {
        let Some(path) = &config.environment_root else {
            return Ok(None);
        };
        // Installed environments are not cache payloads and cannot be nested in
        // the collector's root. Same-device placement preserves native links.
        if !path.is_absolute() || path.starts_with(&config.root) || config.root.starts_with(path) {
            return Err(CacheError::Configuration);
        }
        if initialize && !path.exists() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(path)?;
        }
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.is_dir()
            || metadata.is_symlink()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o077 != 0
            || fs::canonicalize(path)? != *path
            || metadata.dev() != fs::metadata(&config.root)?.dev()
        {
            return Err(CacheError::Ownership);
        }
        let dir = Dir::open_ambient_dir(path, ambient_authority())?;
        if initialize && dir.entries()?.next().is_none() {
            let mut marker = dir.open_with(
                ".nds-environments",
                OpenOptions::new().write(true).create_new(true),
            )?;
            marker.write_all(ENVIRONMENTS)?;
            marker.sync_all()?;
        }
        if regular(&dir, Path::new(".nds-environments"), ENVIRONMENTS.len())? != ENVIRONMENTS {
            return Err(CacheError::Ownership);
        }
        Ok(Some(dir))
    }

    fn uv_environment(
        &self,
        executable: &Path,
        cwd: &Path,
        args: &[OsString],
    ) -> Result<Option<PathBuf>> {
        let Some(environment_root) = &self.config.environment_root else {
            return Ok(None);
        };
        if std::env::var_os("UV_PROJECT_ENVIRONMENT").is_some()
            || args.iter().take_while(|a| a.as_os_str() != "--").any(|a| {
                a == "--active"
                    || a == "--no-project"
                    || a == "--script"
                    || a == "-s"
                    || a.to_str().is_some_and(|s| s.starts_with("--script="))
            })
        {
            return Ok(None);
        }
        let mut arguments = args.iter();
        let command = loop {
            let Some(argument) = arguments.next().and_then(|a| a.to_str()) else {
                return Ok(None);
            };
            if matches!(
                argument,
                "--directory" | "--project" | "--config-file" | "--color" | "--allow-insecure-host"
            ) {
                arguments.next();
            } else if !argument.starts_with('-') {
                break argument;
            }
        };
        if !matches!(command, "run" | "sync" | "add" | "remove") {
            return Ok(None);
        }
        // Ask uv itself, without resolving dependencies or touching the lock.
        // Non-project invocations retain their native behavior.
        let selected = tool_directory("uv", cwd, args)?;
        let Ok(output) = self.native(
            executable,
            vec![
                "workspace".into(),
                "dir".into(),
                "--offline".into(),
                "--directory".into(),
                selected.clone().into_os_string(),
                "--project".into(),
                selected.into_os_string(),
            ],
            Some(cwd.to_owned()),
        ) else {
            return Ok(None);
        };
        let workspace = PathBuf::from(
            String::from_utf8(output)
                .map_err(|_| CacheError::Configuration)?
                .trim_end_matches('\n'),
        );
        if !workspace.is_absolute() || fs::canonicalize(&workspace)? != workspace {
            return Err(CacheError::Configuration);
        }
        let mut identity = b"NDS native project environment v1\0".to_vec();
        identity.extend_from_slice(workspace.as_os_str().as_encoded_bytes());
        let name = digest(&identity);
        let environments =
            Self::environments(&self.config, false)?.ok_or(CacheError::Configuration)?;
        environments.create_dir_all(&name)?;
        let _ = directory(&environments, Path::new(&name))?;
        Ok(Some(environment_root.join(name).join("venv")))
    }

    pub fn initialize(config: Config) -> Result<Self> {
        if !config.root.exists() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&config.root)?;
        }
        let metadata = fs::symlink_metadata(&config.root)?;
        if !metadata.is_dir()
            || metadata.is_symlink()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o077 != 0
            || fs::canonicalize(&config.root)? != config.root
        {
            return Err(CacheError::Ownership);
        }
        let dir = Dir::open_ambient_dir(&config.root, ambient_authority())?;
        if dir.entries()?.next().is_some() {
            return Err(CacheError::Ownership);
        }
        let mut marker = dir.open_with(
            ".nds-cache",
            OpenOptions::new().write(true).create_new(true),
        )?;
        marker.write_all(MARKER)?;
        marker.sync_all()?;
        initialize_directory(&dir)?;
        dir.create_dir_all("cargo/home")?;
        dir.set_permissions(
            "cargo/home",
            cap_std::fs::Permissions::from_std(fs::Permissions::from_mode(0o700)),
        )?;
        dir.create_dir_all("cargo/lanes")?;
        for tool in ["uv", "bun"] {
            dir.create_dir(tool)?;
        }
        Self::environments(&config, true)?;
        Self::open(config)
    }
    pub fn open(config: Config) -> Result<Self> {
        let meta = fs::symlink_metadata(&config.root)?;
        if !meta.is_dir()
            || meta.is_symlink()
            || meta.uid() != rustix::process::geteuid().as_raw()
            || meta.mode() & 0o077 != 0
            || fs::canonicalize(&config.root)? != config.root
        {
            return Err(CacheError::Ownership);
        }
        let root = Dir::open_ambient_dir(&config.root, ambient_authority())?;
        if regular(&root, Path::new(".nds-cache"), MARKER.len())? != MARKER {
            return Err(CacheError::Ownership);
        }
        Self::environments(&config, false)?;
        Ok(Self { config, root })
    }
    fn native(
        &self,
        executable: &Path,
        arguments: Vec<OsString>,
        cwd: Option<PathBuf>,
    ) -> Result<Vec<u8>> {
        let actual = if executable == Path::new("/usr/bin/env") {
            arguments.get(1).map(Path::new).unwrap_or(executable)
        } else {
            executable
        };
        let provider = if actual == self.config.cargo {
            "cargo"
        } else if self.config.uv.as_deref() == Some(actual) {
            "uv"
        } else if self.config.bun.as_deref() == Some(actual) {
            "bun"
        } else if actual == self.config.rustc {
            "rustc"
        } else {
            "native"
        };
        let operation = if arguments.iter().any(|a| a == "--version" || a == "-vV") {
            "version"
        } else if arguments.iter().any(|a| a == "gc") {
            "source_gc"
        } else if arguments.iter().any(|a| a == "clean" || a == "prune") {
            "cache_gc"
        } else {
            "probe"
        };
        let failure = |reason, exit_code| CacheError::Native {
            provider,
            operation,
            reason,
            exit_code,
        };
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| failure("runtime", None))?;
        let io = NativeIo::new(1)
            .map_err(|error| failure(error.code(), None))?
            .process_handle();
        let output = rt
            .block_on(io.process(
                ProcessRequest {
                    executable: executable.to_owned(),
                    arguments,
                    directory: cwd,
                    timeout: Duration::from_secs(30),
                    max_output_bytes: 1_048_576,
                },
                CancellationToken::new(),
            ))
            .map_err(|error| failure(error.code(), None))?;
        if !output.status.success() {
            return Err(failure("exit", output.status.code()));
        }
        Ok(output.stdout)
    }
    fn version(&self, executable: &Path, expected: &str) -> Result<()> {
        let bytes = self.native(executable, vec!["--version".into()], None)?;
        if String::from_utf8_lossy(&bytes).trim() != expected {
            return Err(CacheError::Provider);
        }
        Ok(())
    }
    fn cargo_home(&self) -> Result<PathBuf> {
        let path = self
            .config
            .cargo_home
            .clone()
            .unwrap_or_else(|| self.config.root.join("cargo/home"));
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.is_dir()
            || metadata.is_symlink()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o002 != 0
        {
            return Err(CacheError::Ownership);
        }
        Ok(path)
    }
    // Caller-owned staging is preserved. Recognized paths inside our own pool
    // additionally retain their actual lane lease and completed-use record.
    fn retain_managed_path(&self, path: &Path) -> Result<Option<fs::File>> {
        let prefix = self.config.root.join("cargo/lanes");
        let Ok(relative) = path.strip_prefix(prefix) else {
            return Ok(None);
        };
        let parts: Vec<_> = relative.components().map(|part| part.as_os_str()).collect();
        if parts.len() < 2
            || !parts[0].to_str().is_some_and(|s| hash_name(s, 64))
            || (parts[1] != "build" && parts[1] != "targets")
        {
            return Err(CacheError::Ownership);
        }
        if relative
            .components()
            .any(|part| !matches!(part, std::path::Component::Normal(_)))
        {
            return Err(CacheError::Ownership);
        }
        let lane = prepare(&self.root, &Path::new("cargo/lanes").join(parts[0]))?;
        let guard = lease(&lane, false, true)?;
        if parts[1] == "targets" {
            if parts.len() != 4
                || parts[3] != "cache"
                || !parts[2].to_str().is_some_and(|s| hash_name(s, 64))
            {
                return Err(CacheError::Ownership);
            }
            let target = prepare(&lane, &Path::new("targets").join(parts[2]))?;
            use_cache(&target)?;
            target.create_dir_all("cache")?;
        }
        Ok(Some(guard))
    }
    fn command(
        &self,
        executable: &Path,
        environment: Vec<(String, OsString)>,
        args: &[OsString],
    ) -> Command {
        let mut command = Command::new(executable);
        command.args(args);
        for (key, value) in environment {
            command.env(key, value);
        }
        command
    }
    /// Replace the foreground wrapper with its native owner. The shared lease
    /// has CLOEXEC cleared, so native children retain it even if Cargo is killed.
    pub fn run_cargo(&self, args: &[OsString]) -> Result<()> {
        let explicit = args
            .first()
            .and_then(|s| s.to_str())
            .and_then(|s| s.strip_prefix('+'));
        let selected = explicit
            .map(OsString::from)
            .or_else(|| std::env::var_os("RUSTUP_TOOLCHAIN"));
        let args = if explicit.is_some() { &args[1..] } else { args };
        if args
            .iter()
            .take_while(|s| s.as_os_str() != "--")
            .any(|s| s == "clean")
        {
            return Err(CacheError::Configuration);
        }
        self.version(&self.config.cargo, &qualified("cargo")?)?;
        let cwd = fs::canonicalize(std::env::current_dir()?)?;
        let provided_compiler = std::env::var_os("RUSTC");
        let (rustc, rustdoc) = if let Some(compiler) = provided_compiler.as_ref() {
            let rustc = executable(compiler.clone(), &cwd)?;
            let rustdoc = if let Some(value) = std::env::var_os("RUSTDOC") {
                executable(value, &cwd)?
            } else if rustc.with_file_name("rustdoc").is_file() {
                rustc.with_file_name("rustdoc")
            } else {
                self.config.rustdoc.clone()
            };
            (rustc, rustdoc)
        } else if let Some(rustup) = &self.config.rustup {
            let resolve = |tool: &str| -> Result<PathBuf> {
                let mut arguments = Vec::new();
                if let Some(home) = &self.config.rustup_home {
                    let mut variable = OsString::from("RUSTUP_HOME=");
                    variable.push(home);
                    arguments.push(variable);
                }
                arguments.extend([rustup.as_os_str().to_owned(), "which".into()]);
                if let Some(channel) = &selected {
                    if channel.is_empty() || channel.len() > 256 {
                        return Err(CacheError::Configuration);
                    }
                    arguments.extend(["--toolchain".into(), channel.clone()]);
                }
                arguments.push(tool.into());
                let value = self.native(Path::new("/usr/bin/env"), arguments, Some(cwd.clone()))?;
                let path = PathBuf::from(
                    String::from_utf8(value)
                        .map_err(|_| CacheError::Provider)?
                        .trim(),
                );
                if !path.is_absolute() {
                    return Err(CacheError::Provider);
                }
                Ok(path)
            };
            (resolve("rustc")?, resolve("rustdoc")?)
        } else {
            (self.config.rustc.clone(), self.config.rustdoc.clone())
        };
        let version = self.native(&rustc, vec!["-vV".into()], Some(cwd.clone()))?;
        let report = String::from_utf8_lossy(&version);
        let field = |name: &str| {
            report
                .lines()
                .find_map(|line| line.strip_prefix(name))
                .ok_or(CacheError::Provider)
        };
        let release = field("release: ")?;
        let host = field("host: ")?;
        field("commit-hash: ")?;
        if self.config.rustup.is_none() && provided_compiler.is_none() {
            fallback_toolchain(&cwd, release, host)?;
            if selected.as_ref().is_some_and(|channel| {
                channel != release
                    && channel != format!("{release}-{host}").as_str()
                    && channel != "stable"
            }) {
                return Err(CacheError::Configuration);
            }
        }
        let mut identity = version;
        identity.extend(rustc.as_os_str().as_encoded_bytes());
        let lane_name = digest(&identity);
        let lane_path = Path::new("cargo/lanes").join(&lane_name);
        let lane = prepare(&self.root, &lane_path)?;
        let _lease = lease(&lane, false, true)?;
        let inherited_build = std::env::var_os("CARGO_BUILD_BUILD_DIR");
        let special_target = std::env::var_os("CARGO_TARGET_DIR");
        let inherited_target = special_target
            .clone()
            .or_else(|| std::env::var_os("CARGO_BUILD_TARGET_DIR"));
        let parent_build = std::env::var_os("NDS_CLEANER_MANAGED_BUILD_DIR");
        let parent_target = std::env::var_os("NDS_CLEANER_MANAGED_TARGET_DIR");
        // Only recognize dedicated native options, without interpreting Cargo
        // configuration, aliases or arguments forwarded after `--`.
        let mut arguments = args.iter().take_while(|arg| arg.as_os_str() != "--");
        let mut cli_target = None;
        let mut has_cli_target = false;
        let mut has_cli_config = false;
        while let Some(argument) = arguments.next() {
            if argument == "--target-dir" {
                has_cli_target = true;
                cli_target = arguments.next().cloned();
            } else if let Some(value) = argument.as_encoded_bytes().strip_prefix(b"--target-dir=") {
                has_cli_target = true;
                cli_target = Some(OsString::from_vec(value.to_vec()));
            } else if argument == "--config" {
                has_cli_config = true;
                let _ = arguments.next();
            } else if argument.as_encoded_bytes().starts_with(b"--config=") {
                has_cli_config = true;
            }
        }
        let inherited_defaults = parent_build.is_some()
            && (inherited_build.is_none() || inherited_build == parent_build);
        let caller_target = has_cli_target
            || (inherited_target.is_some()
                && !(inherited_defaults && inherited_target == parent_target));
        let nested = inherited_defaults && !caller_target;
        let delegated =
            std::env::var_os("NDS_CLEANER_EXTERNAL").as_deref() == Some(std::ffi::OsStr::new("1"));
        let depth = if nested {
            std::env::var("NDS_CLEANER_NESTING")
                .unwrap_or_else(|_| "0".into())
                .parse::<u8>()
                .map_err(|_| CacheError::Configuration)?
                .checked_add(u8::from(!delegated))
                .ok_or(CacheError::Configuration)?
        } else {
            0
        };
        if depth > 8 {
            return Err(CacheError::Configuration);
        }
        // Do not reuse the former fine-grain layout. Its dirty-unit lock
        // upgrades can deadlock independent Cargo processes (#17508 upstream).
        // Sources/downloads still share CARGO_HOME; native output locks belong
        // to one workspace, not the whole compiler lane.
        let mut workspace_identity = b"cargo-workspace-output-v2\0".to_vec();
        workspace_identity.extend(cwd.as_os_str().as_encoded_bytes());
        for (index, argument) in args.iter().enumerate() {
            if argument == "--manifest-path" {
                if let Some(value) = args.get(index + 1) {
                    workspace_identity.push(0);
                    workspace_identity.extend(value.as_encoded_bytes());
                }
            } else if argument
                .to_str()
                .is_some_and(|value| value.starts_with("--manifest-path="))
            {
                workspace_identity.push(0);
                workspace_identity.extend(argument.as_encoded_bytes());
            }
        }
        workspace_identity.push(depth);
        let workspace = digest(&workspace_identity);
        let target_path = lane_path.join("targets").join(&workspace);
        directory(&self.root, Path::new("cargo/home"))?;
        let root = &self.config.root;
        let managed_target = root.join(&target_path).join("cache");
        let managed_build = managed_target.clone();
        let default_build = !caller_target && (nested || inherited_build.is_none());
        let default_target = !caller_target;
        let clear_parent_build = caller_target && inherited_defaults && inherited_build.is_some();
        // With caller placement, omit our build-dir default altogether. Cargo
        // chooses an independent configured build-dir, or defaults to its actual
        // selected target; neither choice can be inferred from the target alone.
        let known_build = if has_cli_config || clear_parent_build {
            None
        } else if default_build {
            Some(managed_build.as_os_str().to_owned())
        } else {
            inherited_build
        };
        let known_target = if has_cli_target {
            cli_target
        } else if special_target.is_some() && !default_target {
            special_target
        } else if has_cli_config {
            None
        } else if default_target {
            Some(managed_target.as_os_str().to_owned())
        } else {
            inherited_target
        };
        // Config-file origins/templates remain conservative. Do not stamp
        // hypothetical default paths as actual use after caller configuration.
        let _build_lease = known_build
            .as_deref()
            .filter(|path| !path.as_encoded_bytes().contains(&b'{'))
            .map(|path| self.retain_managed_path(Path::new(path)))
            .transpose()?;
        let _target_lease = known_target
            .as_deref()
            .filter(|path| !path.is_empty())
            .map(|path| self.retain_managed_path(Path::new(path)))
            .transpose()?;
        let source_home =
            std::env::var_os("CARGO_HOME").unwrap_or(self.cargo_home()?.into_os_string());
        let compiler_bin = rustc.parent().ok_or(CacheError::Provider)?;
        let external_command = external_command(args);
        let external = match external_command.map(|(_, name)| name) {
            Some("clippy") => Some(compiler_bin.join("cargo-clippy")),
            Some("nextest") => {
                let bin = Path::new(&source_home).join("bin");
                let paths: Vec<_> =
                    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect();
                let candidate = bin.join("cargo-nextest");
                if !paths.contains(&bin) && candidate.is_file() {
                    Some(candidate)
                } else {
                    Some(executable("cargo-nextest".into(), &cwd)?)
                }
            }
            _ => None,
        };
        let path = std::env::join_paths(
            self.config
                .frontends
                .iter()
                .cloned()
                .chain(std::iter::once(compiler_bin.to_owned()))
                .chain(std::env::split_paths(
                    &std::env::var_os("PATH").unwrap_or_default(),
                )),
        )
        .map_err(|_| CacheError::Configuration)?;
        let frontend = self
            .config
            .frontends
            .as_ref()
            .map(|path| path.join("cargo"));
        if frontend.as_ref().is_some_and(|path| !path.is_file()) {
            return Err(CacheError::Configuration);
        }
        let mut env = vec![
            ("CARGO_HOME".into(), source_home),
            ("NDS_CLEANER_NESTING".into(), depth.to_string().into()),
            ("CARGO_UNSTABLE_MTIME_ON_USE".into(), "true".into()),
            // Keep Cargo's normal output-directory lock. The experimental
            // unit-lock mode is not a safe cross-process cache boundary.
            ("CARGO_UNSTABLE_FINE_GRAIN_LOCKING".into(), "false".into()),
            ("RUSTC".into(), rustc.into_os_string()),
            ("RUSTDOC".into(), rustdoc.into_os_string()),
            ("PATH".into(), path),
        ];
        if external.is_some() && frontend.is_none() {
            return Err(CacheError::Configuration);
        }
        if let Some(frontend) = frontend {
            env.push(("CARGO".into(), frontend.into_os_string()));
        }
        if let Some(channel) = selected {
            env.push(("RUSTUP_TOOLCHAIN".into(), channel));
        }
        if let Some(home) = &self.config.rustup_home {
            env.push(("RUSTUP_HOME".into(), home.as_os_str().to_owned()));
        }
        let mut native_args = vec!["-Zmtime-on-use".into(), "-Zgc".into()];
        // Invocation-local configuration avoids propagating generated final-
        // output locks to native Cargo invoked directly by build scripts.
        for (default, key, path) in [
            (default_build, "build.build-dir", &managed_build),
            (default_target, "build.target-dir", &managed_target),
        ] {
            if default {
                native_args.push("--config".into());
                let path = path.to_str().ok_or(CacheError::Configuration)?;
                native_args.push(
                    format!(
                        "{key}={}",
                        serde_json::to_string(path).map_err(|_| CacheError::Configuration)?
                    )
                    .into(),
                );
            }
        }
        // Cargo has separate root/subcommand config matchers. A caller's
        // post-command --config can otherwise hide all generated root defaults.
        // Keep every config in one matcher, preserving caller override order.
        let mut remaining = Vec::new();
        let mut arguments = args.iter();
        while let Some(argument) = arguments.next() {
            if argument == "--" {
                remaining.push(argument.clone());
                remaining.extend(arguments.cloned());
                break;
            }
            if argument == "--config" {
                native_args.push(argument.clone());
                native_args.push(arguments.next().ok_or(CacheError::Configuration)?.clone());
            } else if argument.as_encoded_bytes().starts_with(b"--config=") {
                native_args.push(argument.clone());
            } else {
                remaining.push(argument.clone());
            }
        }
        native_args.extend(remaining);
        // Cargo rewrites CARGO for external commands and drops invocation-local
        // config. Dispatch these qualified consumers with the frontend instead;
        // their Cargo children re-enter the same scope, without exporting final-
        // output paths into build scripts or acquiring another nesting level.
        let is_external = external.is_some();
        let mut command = if let Some(executable) = external {
            let (index, name) = external_command.ok_or(CacheError::Configuration)?;
            let mut forwarded = vec![args[index].clone()];
            let mut rest = &args[index + 1..];
            if name == "nextest"
                && rest
                    .first()
                    .is_some_and(|arg| arg == "run" || arg == "list")
            {
                forwarded.push(rest[0].clone());
                rest = &rest[1..];
            }
            forwarded.extend_from_slice(&args[..index]);
            forwarded.extend_from_slice(rest);
            self.command(&executable, env, &forwarded)
        } else {
            self.command(&self.config.cargo, env, &native_args)
        };
        command.env_remove("NDS_CLEANER_EXTERNAL");
        if is_external {
            command.env("NDS_CLEANER_EXTERNAL", "1");
        }
        // Generated-default hints carry recursion context, not proof of the
        // effective paths after caller configuration. Keep that context even
        // with CLI config; only known_* above governs additional path retention.
        for (key, generated_default, path) in [
            (
                "NDS_CLEANER_MANAGED_BUILD_DIR",
                default_build,
                &managed_build,
            ),
            (
                "NDS_CLEANER_MANAGED_TARGET_DIR",
                default_target,
                &managed_target,
            ),
        ] {
            if generated_default {
                command.env(key, path);
            } else {
                command.env_remove(key);
            }
        }
        if default_build || clear_parent_build {
            command.env_remove("CARGO_BUILD_BUILD_DIR");
        }
        if default_target {
            command
                .env_remove("CARGO_TARGET_DIR")
                .env_remove("CARGO_BUILD_TARGET_DIR");
        }
        Err(command.exec().into())
    }

    pub fn run_tool(&self, config_path: &Path, tool: &str, args: &[OsString]) -> Result<()> {
        #[cfg(not(target_os = "linux"))]
        let _ = config_path;
        let (executable, variable) = match tool {
            "uv" => (self.config.uv.as_ref(), "UV_CACHE_DIR"),
            "bun" => (self.config.bun.as_ref(), "BUN_INSTALL_CACHE_DIR"),
            _ => return Err(CacheError::Configuration),
        };
        if args
            .iter()
            .take_while(|arg| arg.as_os_str() != "--")
            .any(|a| a == "cache" || a.to_str().is_some_and(|a| a.starts_with("--cache-dir")))
        {
            return Err(CacheError::Configuration);
        }
        let executable = executable.ok_or(CacheError::Provider)?;
        self.version(executable, &qualified(tool)?)?;
        let cwd = fs::canonicalize(std::env::current_dir()?)?;
        let shard_path = Path::new(tool).join(tool_generation(tool, &cwd, args, now()?)?);
        let shard = prepare(&self.root, &shard_path)?;
        let _lease = lease(&shard, false, true)?;
        use_cache(&shard)?;
        shard.create_dir_all("cache")?;
        #[cfg(target_os = "linux")]
        if tool == "bun" {
            return self.bun_guard(config_path, &shard_path, &shard, args);
        }
        let mut environment = vec![(
            variable.into(),
            self.config
                .root
                .join(shard_path)
                .join("cache")
                .into_os_string(),
        )];
        if tool == "uv"
            && let Some(path) = self.uv_environment(executable, &cwd, args)?
        {
            environment.push(("UV_PROJECT_ENVIRONMENT".into(), path.into_os_string()));
        }
        Err(self.command(executable, environment, args).exec().into())
    }
}

#[derive(Default, Serialize)]
pub struct Report {
    pub schema: u8,
    pub dry_run: bool,
    pub retention_seconds: u64,
    pub stale_units: usize,
    pub removed_units: usize,
    pub busy: usize,
    pub unknown: usize,
    pub native_cache_runs: usize,
    pub empty_units: usize,
    pub source_gc_deferred: bool,
    pub completed_uses: usize,
    pub scan_complete: bool,
    pub resumed: bool,
}
struct Budget {
    entries: usize,
    limit: usize,
    start: Instant,
    wall_start: Instant,
}
impl Budget {
    fn new() -> Self {
        Self {
            entries: 0,
            limit: MAX_ENTRIES,
            start: Instant::now(),
            wall_start: Instant::now(),
        }
    }
    fn visit(&mut self) -> Result<()> {
        self.entries += 1;
        if self.entries > self.limit
            || self.start.elapsed() > Duration::from_secs(20)
            || self.wall_start.elapsed() > Duration::from_secs(60)
        {
            Err(CacheError::Budget)
        } else {
            Ok(())
        }
    }

    // Bound discovery, not the work it discovers. Native providers have their
    // own process deadline; deleting a qualified unit or acknowledging a use
    // must not exhaust the next directory's traversal budget.
    fn effect<T>(&mut self, operation: impl FnOnce() -> Result<T>) -> Result<T> {
        let start = Instant::now();
        let result = operation();
        self.start += start.elapsed();
        result
    }
}

/// uv leaves its stable native lock after cleaning a generation. Retain that
/// lock and the generation ledger; an empty store has no payload to collect.
fn uv_cache_empty(cache: &Dir, budget: &mut Budget) -> Result<bool> {
    for entry in cache.entries()? {
        budget.visit()?;
        let entry = entry?;
        if entry.file_name() != ".lock"
            || !entry.file_type()?.is_file()
            || entry.metadata()?.len() != 0
        {
            return Ok(false);
        }
    }
    Ok(true)
}

type ScanKey = Vec<String>;
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Progress {
    schema: u8,
    after: ScanKey,
}
struct Scan {
    budget: Budget,
    retention: Duration,
    after: Option<ScanKey>,
    position: Option<ScanKey>,
}
impl Scan {
    fn new(after: Option<ScanKey>) -> Self {
        Self {
            budget: Budget::new(),
            retention: RETENTION,
            position: after.clone(),
            after,
        }
    }
    fn load(root: &Dir, apply: bool) -> Result<Self> {
        match root.symlink_metadata(".gc-progress") {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Self::new(None)),
            Err(error) => Err(error.into()),
            Ok(_) => {
                let value: Progress =
                    serde_json::from_slice(&regular(root, Path::new(".gc-progress"), 16_384)?)
                        .map_err(|_| CacheError::Ownership)?;
                if value.schema != 1
                    || value.after.is_empty()
                    || value.after.len() > 24
                    || !matches!(value.after[0].as_str(), "0" | "1" | "2")
                    || value.after.iter().enumerate().any(|(i, s)| {
                        s.is_empty()
                            || s.len() > 510
                            || (!hash_name(s, s.len()) && !(s == "g" && i + 1 == value.after.len()))
                    })
                {
                    return Err(CacheError::Ownership);
                }
                Ok(Self::new(apply.then_some(value.after)))
            }
        }
    }
    fn skip(&self, key: &ScanKey) -> bool {
        self.after.as_ref().is_some_and(|after| {
            let finished =
                after.last().is_some_and(|s| s == "g") && &after[..after.len() - 1] == key;
            finished || (key <= after && (key == after || !after.starts_with(key)))
        })
    }
    fn note(&mut self, key: &ScanKey) {
        if self.position.as_ref().is_none_or(|position| position < key) {
            self.position = Some(key.clone());
        }
    }
    fn finish(&mut self, key: &ScanKey) {
        // Components encode bytes as hex. `g` sorts after every descendant and
        // records a completed subtree, including one whose last unit was removed.
        let mut end = key.clone();
        end.push("g".into());
        self.note(&end);
    }
    fn save(&self, root: &Dir) -> Result<()> {
        let Some(after) = &self.position else {
            return Ok(());
        };
        let pending = format!(".gc-progress-{}-{}", std::process::id(), now()?);
        let mut file = root.open_with(&pending, OpenOptions::new().write(true).create_new(true))?;
        file.set_permissions(cap_std::fs::Permissions::from_std(
            fs::Permissions::from_mode(0o600),
        ))?;
        file.write_all(
            &serde_json::to_vec(&Progress {
                schema: 1,
                after: after.clone(),
            })
            .map_err(|_| CacheError::Configuration)?,
        )?;
        file.sync_all()?;
        root.rename(&pending, root, ".gc-progress")?;
        Ok(())
    }
    fn clear(root: &Dir) -> Result<()> {
        match root.remove_file(".gc-progress") {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }
}
fn scan_key(prefix: &[String], name: &std::ffi::OsStr) -> ScanKey {
    let mut key = prefix.to_vec();
    key.push(
        name.as_encoded_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
    );
    key
}
fn sorted_entries(dir: &Dir) -> Result<Vec<cap_std::fs::DirEntry>> {
    let mut entries = dir
        .entries()?
        .take(MAX_ENTRIES + 1)
        .collect::<io::Result<Vec<_>>>()?;
    if entries.len() > MAX_ENTRIES {
        return Err(CacheError::Budget);
    }
    entries.sort_by_key(|entry| entry.file_name());
    Ok(entries)
}

fn stamp(dir: &Dir) -> Result<Option<SystemTime>> {
    let fingerprints = match directory(dir, Path::new("fingerprint")) {
        Ok(dir) => dir,
        Err(_) => return Ok(None),
    };
    let mut newest = None;
    for (index, entry) in fingerprints.entries()?.enumerate() {
        if index >= 256 {
            return Ok(None);
        }
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if name.ends_with(".json")
            || ![
                "lib-",
                "bin-",
                "test-",
                "example-",
                "build-script-",
                "run-build-script-",
            ]
            .iter()
            .any(|prefix| name.starts_with(prefix))
        {
            continue;
        }
        let bytes = match regular(&fingerprints, Path::new(name), 16) {
            Ok(bytes) => bytes,
            Err(CacheError::Ownership) => return Ok(None),
            Err(error) => return Err(error),
        };
        if bytes.len() != 16 || !bytes.iter().all(u8::is_ascii_hexdigit) {
            return Ok(None);
        }
        let modified = fingerprints.open(name)?.metadata()?.modified()?.into_std();
        newest = Some(newest.map_or(modified, |last: SystemTime| last.max(modified)));
    }
    Ok(newest)
}
fn artifact_units(
    dir: &Dir,
    depth: usize,
    current: u64,
    apply: bool,
    report: &mut Report,
    scan: &mut Scan,
    prefix: &[String],
) -> Result<()> {
    for entry in sorted_entries(dir)? {
        let key = scan_key(prefix, &entry.file_name());
        if scan.skip(&key) {
            continue;
        }
        scan.budget.visit()?;
        if !entry.file_type()?.is_dir() {
            scan.note(&key);
            continue;
        }
        let name = entry.file_name();
        let Some(name_str) = name.to_str() else {
            report.unknown += 1;
            scan.note(&key);
            continue;
        };
        // These contain native payloads, not the unit/fingerprint layout we
        // collect. Incremental objects can vastly exceed the discovery limit.
        // Retain these directories without walking their contents.
        if matches!(name_str, "incremental" | "deps" | "examples") {
            report.unknown += 1;
            scan.note(&key);
            continue;
        }
        let child = directory(dir, Path::new(&name))?;
        if name_str == "build" {
            for package in sorted_entries(&child)? {
                let package_key = scan_key(&key, &package.file_name());
                if scan.skip(&package_key) {
                    continue;
                }
                scan.budget.visit()?;
                if !package.file_type()?.is_dir() {
                    scan.note(&package_key);
                    continue;
                }
                let package_dir = directory(&child, Path::new(&package.file_name()))?;
                for unit in sorted_entries(&package_dir)? {
                    let unit_key = scan_key(&package_key, &unit.file_name());
                    if scan.skip(&unit_key) {
                        continue;
                    }
                    scan.budget.visit()?;
                    let name = unit.file_name();
                    if !unit.file_type()?.is_dir()
                        || !name.to_str().is_some_and(|s| hash_name(s, 16))
                    {
                        report.unknown += 1;
                        scan.note(&unit_key);
                        continue;
                    }
                    let unit_dir = directory(&package_dir, Path::new(&name))?;
                    if let Some(modified) = stamp(&unit_dir)? {
                        let seconds = modified
                            .duration_since(UNIX_EPOCH)
                            .map_err(|_| CacheError::Ownership)?
                            .as_secs();
                        if expired(seconds, current, scan.retention) {
                            report.stale_units += 1;
                            if apply {
                                scan.budget.effect(|| Ok(unit_dir.remove_open_dir_all()?))?;
                                report.removed_units += 1;
                            }
                        }
                    } else {
                        report.unknown += 1;
                    }
                    scan.note(&unit_key);
                }
                scan.finish(&package_key);
            }
        } else if depth < 8 && !name_str.starts_with('.') {
            artifact_units(&child, depth + 1, current, apply, report, scan, &key)?;
        }
        scan.finish(&key);
    }
    Ok(())
}
impl ManagedCache {
    pub fn collect(&self, apply: bool) -> Result<Report> {
        self.collect_at(apply, now()?)
    }
    fn collect_at(&self, apply: bool, current: u64) -> Result<Report> {
        let _cleaner = lease(&self.root, true, false)?;
        self.version(&self.config.cargo, &qualified("cargo")?)?;
        let mut report = Report {
            schema: 1,
            dry_run: !apply,
            retention_seconds: self.config.retention().as_secs(),
            scan_complete: true,
            ..Report::default()
        };
        let mut scan = Scan::load(&self.root, apply)?;
        scan.retention = self.config.retention();
        scan.budget.limit = self.config.scan_entries.unwrap_or(MAX_ENTRIES);
        report.resumed = scan.after.is_some();
        match self.collect_pass(apply, current, &mut report, &mut scan) {
            Ok(()) => {
                if apply {
                    Scan::clear(&self.root)?;
                }
            }
            Err(CacheError::Budget) => {
                report.scan_complete = false;
                report.source_gc_deferred = true;
                if apply {
                    scan.save(&self.root)?;
                }
            }
            Err(error) => return Err(error),
        }
        Ok(report)
    }
    fn collect_pass(
        &self,
        apply: bool,
        current: u64,
        report: &mut Report,
        scan: &mut Scan,
    ) -> Result<()> {
        let lanes = directory(&self.root, Path::new("cargo/lanes"))?;
        let mut cargo_busy = false;
        for entry in sorted_entries(&lanes)? {
            scan.budget.visit()?;
            let name = entry.file_name();
            let key = scan_key(&["0".into()], &name);
            if !entry.file_type()?.is_dir() || !name.to_str().is_some_and(|s| hash_name(s, 64)) {
                report.unknown += 1;
                scan.note(&key);
                continue;
            }
            let lane = directory(&lanes, Path::new(&name))?;
            let _lease = match lease(&lane, true, false) {
                Ok(lease) => lease,
                Err(CacheError::Busy) => {
                    report.busy += 1;
                    cargo_busy = true;
                    scan.note(&key);
                    continue;
                }
                Err(CacheError::Uninitialized) => {
                    report.unknown += 1;
                    cargo_busy = true;
                    scan.note(&key);
                    continue;
                }
                Err(error) => return Err(error),
            };
            if scan.skip(&key) {
                continue;
            }
            if let Ok(build) = directory(&lane, Path::new("build")) {
                let mut prefix = key.clone();
                prefix.push("0".into());
                if !scan.skip(&prefix) {
                    artifact_units(&build, 0, current, apply, report, scan, &prefix)?;
                }
            }
            if let Ok(targets) = directory(&lane, Path::new("targets")) {
                let mut prefix = key.clone();
                prefix.push("1".into());
                for entry in sorted_entries(&targets)? {
                    let target_key = scan_key(&prefix, &entry.file_name());
                    if scan.skip(&target_key) {
                        continue;
                    }
                    scan.budget.visit()?;
                    let name = entry.file_name();
                    if !entry.file_type()?.is_dir()
                        || !name.to_str().is_some_and(|s| hash_name(s, 64))
                    {
                        report.unknown += 1;
                        scan.note(&target_key);
                        continue;
                    }
                    let target = directory(&targets, Path::new(&name))?;
                    if scan
                        .budget
                        .effect(|| completed_use(&target, current, apply))?
                    {
                        report.completed_uses += 1;
                        scan.note(&target_key);
                        continue;
                    }
                    match last_use(&target) {
                        Ok(last) if expired(last, current, scan.retention) => {
                            report.stale_units += 1;
                            if apply {
                                scan.budget.effect(|| Ok(target.remove_open_dir_all()?))?;
                                report.removed_units += 1;
                            }
                        }
                        Ok(_) => {}
                        Err(_) => report.unknown += 1,
                    }
                    scan.note(&target_key);
                }
            }
            scan.finish(&key);
        }
        for (tool, executable, variable) in [
            ("uv", self.config.uv.as_ref(), "UV_CACHE_DIR"),
            ("bun", self.config.bun.as_ref(), "BUN_INSTALL_CACHE_DIR"),
        ] {
            let shards = directory(&self.root, Path::new(tool))?;
            let mut provider_qualified = false;
            for entry in sorted_entries(&shards)? {
                let key = scan_key(
                    &[if tool == "uv" { "1" } else { "2" }.into()],
                    &entry.file_name(),
                );
                if scan.skip(&key) {
                    continue;
                }
                scan.budget.visit()?;
                let name = entry.file_name();
                if !entry.file_type()?.is_dir() || !name.to_str().is_some_and(|s| hash_name(s, 64))
                {
                    report.unknown += 1;
                    scan.note(&key);
                    continue;
                }
                let shard = directory(&shards, Path::new(&name))?;
                let _lease = match lease(&shard, true, false) {
                    Ok(lease) => lease,
                    Err(CacheError::Busy) => {
                        report.busy += 1;
                        scan.note(&key);
                        continue;
                    }
                    Err(CacheError::Uninitialized) => {
                        report.unknown += 1;
                        scan.note(&key);
                        continue;
                    }
                    Err(error) => return Err(error),
                };
                if tool == "bun" && !bun_completed(&shard).unwrap_or(false) {
                    report.unknown += 1;
                    scan.note(&key);
                    continue;
                }
                let cache_dir = match directory(&shard, Path::new("cache")) {
                    Ok(dir) => dir,
                    Err(_) => {
                        report.unknown += 1;
                        scan.note(&key);
                        continue;
                    }
                };
                if tool == "uv" && !native_locks_available(&cache_dir, &[".lock"])? {
                    observe_native_use(&shard, current, apply)?;
                    report.busy += 1;
                    scan.note(&key);
                    continue;
                }
                if scan
                    .budget
                    .effect(|| completed_use(&shard, current, apply))?
                {
                    report.completed_uses += 1;
                    scan.note(&key);
                    continue;
                }
                let last = match last_use(&shard) {
                    Ok(last) => last,
                    Err(_) => {
                        report.unknown += 1;
                        scan.note(&key);
                        continue;
                    }
                };
                if !expired(last, current, scan.retention) {
                    scan.note(&key);
                    continue;
                }
                if tool == "uv" && uv_cache_empty(&cache_dir, &mut scan.budget)? {
                    report.empty_units += 1;
                    scan.note(&key);
                    continue;
                }
                report.stale_units += 1;
                if !apply {
                    scan.note(&key);
                    continue;
                }
                let executable = executable.ok_or(CacheError::Provider)?;
                if !provider_qualified {
                    scan.budget
                        .effect(|| self.version(executable, &qualified(tool)?))?;
                    provider_qualified = true;
                }
                directory(&shard, Path::new("cache"))?;
                let mut env = OsString::from(format!("{variable}="));
                env.push(self.config.root.join(tool).join(&name).join("cache"));
                let mut args = vec![env, executable.as_os_str().to_owned()];
                args.extend(if tool == "uv" {
                    vec!["cache".into(), "clean".into()]
                } else {
                    vec![
                        "pm".into(),
                        "cache".into(),
                        "prune".into(),
                        "--max-age".into(),
                        (scan.retention.as_secs() / 86_400).to_string().into(),
                    ]
                });
                match scan
                    .budget
                    .effect(|| self.native(Path::new("/usr/bin/env"), args, None))
                {
                    Ok(_) => {}
                    Err(CacheError::Native {
                        reason: "timeout", ..
                    }) if tool == "uv" && !native_locks_available(&cache_dir, &[".lock"])? => {
                        observe_native_use(&shard, current, apply)?;
                        report.stale_units -= 1;
                        report.busy += 1;
                        scan.note(&key);
                        continue;
                    }
                    Err(error) => return Err(error),
                }
                report.native_cache_runs += 1;
                scan.note(&key);
                // Keep ledger and native Bun store links. No raw cache tree removal.
            }
        }
        if cargo_busy {
            report.source_gc_deferred = true;
            return Ok(());
        }
        let cargo_home = self.cargo_home()?;
        let home = Dir::open_ambient_dir(&cargo_home, ambient_authority())?;
        let locks = &[".package-cache", ".package-cache-mutate"];
        if !native_locks_available(&home, locks)? {
            report.busy += 1;
            report.source_gc_deferred = true;
            return Ok(());
        }
        scan.budget.visit()?;
        let mut env = OsString::from("CARGO_HOME=");
        env.push(cargo_home);
        let mut args = vec![
            env,
            self.config.cargo.as_os_str().to_owned(),
            "-Zgc".into(),
            "clean".into(),
            "gc".into(),
        ];
        for option in [
            "--max-src-age",
            "--max-crate-age",
            "--max-index-age",
            "--max-git-co-age",
            "--max-git-db-age",
        ] {
            args.push(format!("{option}={}days", scan.retention.as_secs() / 86_400).into());
        }
        if !apply {
            args.push("--dry-run".into());
        }
        match self.native(Path::new("/usr/bin/env"), args, None) {
            Ok(_) => {}
            Err(CacheError::Native {
                reason: "timeout", ..
            }) if !native_locks_available(&home, locks)? => {
                report.busy += 1;
                report.source_gc_deferred = true;
                return Ok(());
            }
            Err(error) => return Err(error),
        }
        report.native_cache_runs += 1;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs::FileTimes, os::unix::fs::symlink, process::Stdio};
    fn fixture() -> (tempfile::TempDir, Config) {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap().join("managed");
        (
            temp,
            Config {
                schema: 1,
                root,
                cargo: "/nonexistent/cargo".into(),
                rustc: "/nonexistent/rustc".into(),
                rustdoc: "/nonexistent/rustdoc".into(),
                rustup: None,
                rustup_home: None,
                frontends: None,
                cargo_home: None,
                uv: None,
                bun: None,
                scan_entries: None,
                retention_seconds: None,
                environment_root: None,
            },
        )
    }
    fn unit(base: &Path, name: &str, stamp_age: u64) -> PathBuf {
        let path = base.join("debug/build/package").join(name);
        fs::create_dir_all(path.join("fingerprint")).unwrap();
        let fingerprint = path.join("fingerprint/lib-package");
        fs::write(&fingerprint, b"1234567890abcdef").unwrap();
        fs::File::open(&fingerprint)
            .unwrap()
            .set_times(
                FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(stamp_age)),
            )
            .unwrap();
        fs::write(path.join("rebuildable.rlib"), b"artifact").unwrap();
        path
    }
    fn scan(root: &Path, apply: bool) -> Report {
        let dir = Dir::open_ambient_dir(root, ambient_authority()).unwrap();
        let mut report = Report::default();
        artifact_units(
            &dir,
            0,
            now().unwrap(),
            apply,
            &mut report,
            &mut Scan::new(None),
            &[],
        )
        .unwrap();
        report
    }
    #[test]
    fn retention_is_strict_and_clock_rollback_preserves() {
        assert!(!expired(100, 100 + RETENTION.as_secs(), RETENTION));
        assert!(expired(100, 101 + RETENTION.as_secs(), RETENTION));
        assert!(!expired(100, 99, RETENTION));
        let one_day = Duration::from_secs(86_400);
        assert!(!expired(100, 86_500, one_day));
        assert!(expired(100, 86_501, one_day));
        assert!(!expired(100, 99, one_day));
    }
    #[test]
    fn native_failure_keeps_provider_operation_and_exit_category() {
        let (_temp, mut config) = fixture();
        config.cargo = "/usr/bin/false".into();
        let cache = ManagedCache::initialize(config).unwrap();
        assert!(matches!(
            cache.collect(false),
            Err(CacheError::Native {
                provider: "cargo",
                operation: "version",
                reason: "exit",
                exit_code: Some(1)
            })
        ));
    }
    #[test]
    fn empty_uv_detection_keeps_native_lock_identity_and_refuses_unknown_payload() {
        let (temp, _) = fixture();
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        assert!(uv_cache_empty(&dir, &mut Budget::new()).unwrap());
        fs::write(temp.path().join(".lock"), b"").unwrap();
        let before = fs::metadata(temp.path().join(".lock")).unwrap();
        assert!(uv_cache_empty(&dir, &mut Budget::new()).unwrap());
        let after = fs::metadata(temp.path().join(".lock")).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        fs::write(temp.path().join("payload"), b"keep").unwrap();
        assert!(!uv_cache_empty(&dir, &mut Budget::new()).unwrap());
        fs::remove_file(temp.path().join("payload")).unwrap();
        fs::write(temp.path().join(".lock"), b"unknown").unwrap();
        assert!(!uv_cache_empty(&dir, &mut Budget::new()).unwrap());
        fs::remove_file(temp.path().join(".lock")).unwrap();
        std::os::unix::fs::symlink("unknown", temp.path().join(".lock")).unwrap();
        assert!(!uv_cache_empty(&dir, &mut Budget::new()).unwrap());
        assert!(temp.path().join(".lock").is_symlink());
    }
    #[test]
    fn configured_idle_policy_collects_only_qualified_older_units() {
        let (temp, _) = fixture();
        let old = unit(temp.path(), "0123456789abcdef", 2 * 86_400);
        let recent = unit(temp.path(), "fedcba9876543210", 12 * 3600);
        assert_eq!(scan(temp.path(), false).stale_units, 0);
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        let mut cursor = Scan::new(None);
        cursor.retention = Duration::from_secs(86_400);
        let mut report = Report::default();
        artifact_units(&dir, 0, now().unwrap(), true, &mut report, &mut cursor, &[]).unwrap();
        assert_eq!(report.removed_units, 1);
        assert!(!old.exists());
        assert!(recent.exists());
    }
    #[test]
    fn configured_retention_uses_whole_native_days_and_preserves_legacy_default() {
        let (temp, config) = fixture();
        let path = temp.path().join("config.json");
        for seconds in [
            None,
            Some(0),
            Some(3600),
            Some(86_401),
            Some(86_400),
            Some(259_200),
            Some(366 * 86_400),
        ] {
            let mut value = serde_json::json!({
                "schema": 1, "root": config.root, "cargo": config.cargo,
                "rustc": config.rustc, "rustdoc": config.rustdoc
            });
            if let Some(seconds) = seconds {
                value["retention_seconds"] = seconds.into();
            }
            fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
            let parsed = read_config(&path);
            if seconds.is_none_or(|s| s == 86_400 || s == 259_200) {
                assert_eq!(
                    parsed.unwrap().retention().as_secs(),
                    seconds.unwrap_or(RETENTION.as_secs())
                );
            } else {
                assert!(matches!(parsed, Err(CacheError::Configuration)));
            }
        }
    }
    #[test]
    fn locked_generation_reuses_flags_and_member_directory_but_isolates_lock_changes() {
        let (temp, _) = fixture();
        let workspace = temp.path().join("project");
        fs::create_dir_all(workspace.join("member")).unwrap();
        fs::write(workspace.join("uv.lock"), b"version = 1\n").unwrap();
        let scope = [
            "--directory".into(),
            workspace.as_os_str().to_owned(),
            "--project".into(),
            workspace.as_os_str().to_owned(),
        ];
        let mut args = scope.to_vec();
        args.extend(["sync".into(), "--locked".into()]);
        let first = tool_generation("uv", temp.path(), &args, 86400).unwrap();
        args.extend(["--offline".into(), "--no-progress".into()]);
        assert_eq!(
            first,
            tool_generation("uv", temp.path(), &args, 86400 * 10).unwrap()
        );
        args[1] = workspace.join("member").into_os_string();
        args[3] = workspace.join("member").into_os_string();
        assert_eq!(
            first,
            tool_generation("uv", temp.path(), &args, 86400).unwrap()
        );
        fs::write(workspace.join("uv.lock"), b"version = 2\n").unwrap();
        assert_ne!(
            first,
            tool_generation("uv", temp.path(), &args, 86400).unwrap()
        );
        symlink(workspace.join("uv.lock"), workspace.join("member/uv.lock")).unwrap();
        assert!(tool_generation("uv", temp.path(), &args, 86400).is_ok());
    }
    #[test]
    fn unlocked_generation_rolls_daily_and_explicit_cwd_selects_native_lock() {
        let (temp, _) = fixture();
        let args = vec![
            "--cwd".into(),
            temp.path().as_os_str().to_owned(),
            "install".into(),
        ];
        let first = tool_generation("bun", temp.path(), &args, 86400).unwrap();
        assert_ne!(
            first,
            tool_generation("bun", temp.path(), &args, 86400 * 2).unwrap()
        );
        fs::write(temp.path().join("bun.lock"), b"{}\n").unwrap();
        let locked = tool_generation("bun", temp.path(), &args, 86400).unwrap();
        assert_ne!(first, locked);
        let mut offline = args.clone();
        offline.extend([
            "--offline".into(),
            "--".into(),
            "--cwd".into(),
            "not-a-directory".into(),
        ]);
        assert_eq!(
            locked,
            tool_generation("bun", temp.path(), &offline, 86400 * 2).unwrap()
        );
    }
    #[test]
    fn initialization_refuses_foreign_contents() {
        let (_temp, config) = fixture();
        fs::create_dir(&config.root).unwrap();
        fs::set_permissions(&config.root, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(config.root.join("work"), b"keep").unwrap();
        assert!(matches!(
            ManagedCache::initialize(config),
            Err(CacheError::Ownership)
        ));
    }
    #[test]
    fn initialization_refuses_symlink_root() {
        let (temp, config) = fixture();
        symlink(temp.path(), &config.root).unwrap();
        assert!(matches!(
            ManagedCache::initialize(config),
            Err(CacheError::Ownership)
        ));
    }
    #[test]
    fn marker_is_required_and_not_synthesized_on_open() {
        let (_temp, config) = fixture();
        let root = config.root.clone();
        let cache = ManagedCache::initialize(config).unwrap();
        fs::write(root.join(".nds-cache"), b"other owner").unwrap();
        assert!(matches!(
            ManagedCache::open(cache.config),
            Err(CacheError::Ownership)
        ));
    }
    #[test]
    fn stale_native_unit_is_removed_as_a_whole() {
        let (temp, _) = fixture();
        let path = unit(temp.path(), "0123456789abcdef", RETENTION.as_secs() + 10);
        let report = scan(temp.path(), true);
        assert_eq!(report.removed_units, 1);
        assert!(!path.exists());
    }
    #[test]
    fn dry_run_preserves_and_fresh_reuse_keeps_old_artifact() {
        let (temp, _) = fixture();
        let old = unit(temp.path(), "0123456789abcdef", RETENTION.as_secs() + 10);
        assert_eq!(scan(temp.path(), false).stale_units, 1);
        assert!(old.exists());
        fs::File::open(old.join("fingerprint/lib-package"))
            .unwrap()
            .set_times(FileTimes::new().set_modified(SystemTime::now()))
            .unwrap();
        assert_eq!(scan(temp.path(), true).removed_units, 0);
        assert!(old.join("rebuildable.rlib").exists());
    }
    #[test]
    fn unknown_fingerprint_and_layout_are_preserved() {
        let (temp, _) = fixture();
        let path = unit(temp.path(), "0123456789abcdef", RETENTION.as_secs() + 10);
        fs::write(path.join("fingerprint/lib-package"), b"unsupported").unwrap();
        let other = unit(temp.path(), "other-layout", RETENTION.as_secs() + 10);
        let oversized = unit(temp.path(), "fedcba9876543210", RETENTION.as_secs() + 10);
        fs::write(oversized.join("fingerprint/lib-package"), [b'x'; 32]).unwrap();
        let report = scan(temp.path(), true);
        assert_eq!(report.removed_units, 0);
        assert_eq!(report.unknown, 3);
        assert!(other.exists());
    }
    #[test]
    fn symlink_unit_cannot_escape_anchored_cache() {
        let (temp, _) = fixture();
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("precious"), b"keep").unwrap();
        fs::create_dir_all(temp.path().join("debug/build/package")).unwrap();
        symlink(
            outside.path(),
            temp.path().join("debug/build/package/0123456789abcdef"),
        )
        .unwrap();
        assert_eq!(scan(temp.path(), true).removed_units, 0);
        assert_eq!(fs::read(outside.path().join("precious")).unwrap(), b"keep");
    }
    #[test]
    fn regular_control_files_never_follow_links() {
        let (temp, _) = fixture();
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        let external = tempfile::NamedTempFile::new().unwrap();
        symlink(external.path(), temp.path().join(".last-use")).unwrap();
        assert!(regular(&dir, Path::new(".last-use"), 32).is_err());
    }
    #[test]
    fn active_shared_lease_blocks_collection() {
        let (_temp, config) = fixture();
        let cache = ManagedCache::initialize(config).unwrap();
        let first = lease(&cache.root, false, false).unwrap();
        assert!(matches!(
            lease(&cache.root, true, false),
            Err(CacheError::Busy)
        ));
        drop(first);
        assert!(lease(&cache.root, true, false).is_ok());
    }
    #[test]
    fn native_child_inherits_lease_after_parent_exit() {
        let (_temp, config) = fixture();
        let cache = ManagedCache::initialize(config).unwrap();
        let inherited = lease(&cache.root, false, true).unwrap();
        let mut child = Command::new("/bin/sh")
            .args(["-c", "sleep 1 & exit 0"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        drop(inherited);
        assert!(child.wait().unwrap().success());
        assert!(matches!(
            lease(&cache.root, true, false),
            Err(CacheError::Busy)
        ));
        let start = Instant::now();
        loop {
            if lease(&cache.root, true, false).is_ok() {
                break;
            }
            assert!(start.elapsed() < Duration::from_secs(5));
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    #[test]
    fn traversal_budget_is_enforced() {
        let mut budget = Budget {
            entries: MAX_ENTRIES,
            start: Instant::now(),
            ..Budget::new()
        };
        assert!(matches!(budget.visit(), Err(CacheError::Budget)));
        let mut budget = Budget {
            entries: 0,
            start: Instant::now() - Duration::from_secs(21),
            ..Budget::new()
        };
        assert!(matches!(budget.visit(), Err(CacheError::Budget)));
        let mut budget = Budget {
            wall_start: Instant::now() - Duration::from_secs(61),
            ..Budget::new()
        };
        assert!(matches!(budget.visit(), Err(CacheError::Budget)));
    }
    #[test]
    fn interrupted_scan_resumes_after_removed_subtrees_without_changing_preview() {
        let (temp, _) = fixture();
        let mut paths = Vec::new();
        for (index, package) in ["a", "a-b", "z"].into_iter().enumerate() {
            let name = format!("{:016x}", index + 1);
            unit(temp.path(), &name, RETENTION.as_secs() + 10);
            let root = temp.path().join("debug/build");
            fs::rename(root.join("package"), root.join(package)).unwrap();
            paths.push(root.join(package).join(name));
        }
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        let prefix = vec!["0".into()];
        let mut removed = 0;
        for pass in 0..3 {
            let mut scan = Scan::load(&dir, true).unwrap();
            assert_eq!(scan.after.is_some(), pass > 0);
            scan.budget.entries = MAX_ENTRIES - 4;
            let mut report = Report::default();
            let result = artifact_units(
                &dir,
                0,
                now().unwrap(),
                true,
                &mut report,
                &mut scan,
                &prefix,
            );
            removed += report.removed_units;
            if pass < 2 {
                assert!(matches!(result, Err(CacheError::Budget)));
                scan.save(&dir).unwrap();
                let before = fs::read(temp.path().join(".gc-progress")).unwrap();
                let mut preview = Scan::load(&dir, false).unwrap();
                assert!(preview.after.is_none());
                preview.budget.entries = MAX_ENTRIES - 4;
                let mut preview_report = Report::default();
                let _ = artifact_units(
                    &dir,
                    0,
                    now().unwrap(),
                    false,
                    &mut preview_report,
                    &mut preview,
                    &prefix,
                );
                assert_eq!(preview_report.removed_units, 0);
                assert_eq!(fs::read(temp.path().join(".gc-progress")).unwrap(), before);
            } else {
                result.unwrap();
                Scan::clear(&dir).unwrap();
            }
        }
        assert_eq!(removed, 3);
        assert!(paths.iter().all(|p| !p.exists()));
        assert!(!temp.path().join(".gc-progress").exists());
    }
    #[test]
    fn malformed_or_symlink_progress_is_not_adopted() {
        let (temp, _) = fixture();
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        let path = temp.path().join(".gc-progress");
        fs::write(&path, br#"{"schema":99,"after":["0"]}"#).unwrap();
        assert!(matches!(Scan::load(&dir, true), Err(CacheError::Ownership)));
        assert!(matches!(
            Scan::load(&dir, false),
            Err(CacheError::Ownership)
        ));
        fs::remove_file(&path).unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        fs::write(outside.path(), b"keep").unwrap();
        symlink(outside.path(), &path).unwrap();
        assert!(matches!(Scan::load(&dir, true), Err(CacheError::Ownership)));
        assert_eq!(fs::read(outside.path()).unwrap(), b"keep");
    }
    #[test]
    fn operator_budget_cannot_raise_the_discovery_bound() {
        let (temp, config) = fixture();
        let path = temp.path().join("config.json");
        for limit in [0, MAX_ENTRIES + 1, 64, MAX_ENTRIES] {
            fs::write(
                &path,
                serde_json::to_vec(&serde_json::json!({
                    "schema": 1, "root": config.root, "cargo": config.cargo,
                    "rustc": config.rustc, "rustdoc": config.rustdoc, "scan_entries": limit
                }))
                .unwrap(),
            )
            .unwrap();
            if limit == 0 || limit > MAX_ENTRIES {
                assert!(matches!(read_config(&path), Err(CacheError::Configuration)));
            } else {
                assert_eq!(read_config(&path).unwrap().scan_entries, Some(limit));
            }
        }
    }
    #[test]
    fn native_payloads_do_not_starve_qualified_unit_collection() {
        let (temp, _) = fixture();
        let old = unit(temp.path(), "0123456789abcdef", RETENTION.as_secs() + 10);
        let mut payloads = Vec::new();
        for name in ["incremental", "deps", "examples"] {
            let payload = temp.path().join("debug").join(name);
            let nested = unit(&payload, "fedcba9876543210", RETENTION.as_secs() + 10);
            for index in 0..32 {
                fs::write(payload.join(format!("object-{index}")), b"keep").unwrap();
            }
            payloads.push((payload, nested));
        }
        let dir = Dir::open_ambient_dir(temp.path(), ambient_authority()).unwrap();
        let mut report = Report::default();
        artifact_units(
            &dir,
            0,
            now().unwrap(),
            true,
            &mut report,
            &mut Scan {
                budget: Budget {
                    entries: MAX_ENTRIES - 16,
                    ..Budget::new()
                },
                ..Scan::new(None)
            },
            &[],
        )
        .unwrap();
        assert_eq!(report.removed_units, 1);
        assert_eq!(report.unknown, 3);
        assert!(!old.exists());
        for (payload, nested) in payloads {
            assert!(nested.join("rebuildable.rlib").exists());
            assert_eq!(fs::read(payload.join("object-31")).unwrap(), b"keep");
        }
    }
    #[test]
    fn effects_keep_their_errors_without_spending_discovery_time() {
        let mut budget = Budget {
            entries: 0,
            start: Instant::now() - Duration::from_secs(19),
            ..Budget::new()
        };
        let result: Result<()> = budget.effect(|| {
            std::thread::sleep(Duration::from_millis(1100));
            Err(CacheError::Provider)
        });
        assert!(matches!(result, Err(CacheError::Provider)));
        assert!(budget.visit().is_ok());
    }
    #[test]
    fn completion_observation_preserves_recent_long_running_work_and_preview_is_read_only() {
        let (_temp, config) = fixture();
        let cache = ManagedCache::initialize(config).unwrap();
        use_cache(&cache.root).unwrap();
        cache.root.write(".last-use", b"1").unwrap();
        assert!(completed_use(&cache.root, RETENTION.as_secs() * 10, false).unwrap());
        assert_eq!(last_use(&cache.root).unwrap(), 1);
        assert!(completed_use(&cache.root, RETENTION.as_secs() * 10, true).unwrap());
        assert_eq!(last_use(&cache.root).unwrap(), RETENTION.as_secs() * 10);
        assert!(!completed_use(&cache.root, RETENTION.as_secs() * 10, true).unwrap());
    }
    #[test]
    fn fixed_compiler_refuses_a_different_project_pin() {
        let (temp, _) = fixture();
        fs::write(
            temp.path().join("rust-toolchain.toml"),
            "[toolchain]\nchannel=\"1.98.0\"\n",
        )
        .unwrap();
        assert!(fallback_toolchain(temp.path(), "1.99.1", "aarch64-apple-darwin").is_err());
        fs::write(
            temp.path().join("rust-toolchain.toml"),
            "[toolchain]\nchannel=\"1.99.1\"\n",
        )
        .unwrap();
        assert!(fallback_toolchain(temp.path(), "1.99.1", "aarch64-apple-darwin").is_ok());
    }
    #[test]
    fn own_use_ledger_is_atomic_and_bounded() {
        let (_temp, config) = fixture();
        let cache = ManagedCache::initialize(config).unwrap();
        use_cache(&cache.root).unwrap();
        assert!(now().unwrap() - last_use(&cache.root).unwrap() <= 1);
        cache.root.write(".last-use", b"invalid").unwrap();
        assert!(last_use(&cache.root).is_err());
    }
}
