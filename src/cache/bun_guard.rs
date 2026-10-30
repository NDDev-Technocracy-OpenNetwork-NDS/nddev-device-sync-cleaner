//! Linux descendant lifetime, independent of Bun's inherited-fd behavior.
//! The guard waits on its own children; it neither scans nor signals other jobs.
use super::*;
use rustix::{
    event::{PollFd, PollFlags, Timespec, poll},
    process::{Pid, PidfdFlags, WaitOptions, pidfd_open, set_child_subreaper, wait},
};
use std::{
    os::{
        linux::net::SocketAddrExt,
        unix::net::{SocketAddr, UnixListener, UnixStream},
    },
    process::Stdio,
};

fn address(token: &str) -> Result<SocketAddr> {
    if !hash_name(token, 64) {
        return Err(CacheError::Configuration);
    }
    Ok(SocketAddr::from_abstract_name(format!("nds-bun-{token}"))?)
}

impl ManagedCache {
    pub(super) fn bun_guard(
        &self,
        config_path: &Path,
        shard_path: &Path,
        shard: &Dir,
        args: &[OsString],
    ) -> Result<()> {
        let token = digest(
            format!(
                "{}:{}:{}",
                std::process::id(),
                now()?,
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(|_| CacheError::Configuration)?
                    .as_nanos()
            )
            .as_bytes(),
        );
        let listener = UnixListener::bind_addr(&address(&token)?)?;
        listener.set_nonblocking(true)?;
        shard.create_dir_all(".bun-runs")?;
        let runs = directory(shard, Path::new(".bun-runs"))?;
        let mut pending =
            runs.open_with(&token, OpenOptions::new().write(true).create_new(true))?;
        pending.write_all(b"1")?;
        pending.sync_all()?;
        // The generation namespace changed: never attach this proof to old data.
        match shard.open_with(
            ".bun-lifetime",
            OpenOptions::new().write(true).create_new(true),
        ) {
            Ok(mut file) => {
                file.write_all(BUN_LIFETIME)?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if regular(shard, Path::new(".bun-lifetime"), BUN_LIFETIME.len())? != BUN_LIFETIME {
                    return Err(CacheError::Ownership);
                }
            }
            Err(error) => return Err(error.into()),
        }
        let child = Command::new(std::env::current_exe()?)
            .arg(fs::canonicalize(config_path)?)
            .arg("bun-guard")
            .arg(shard_path.file_name().ok_or(CacheError::Configuration)?)
            .arg(&token)
            .arg("--")
            .args(args)
            .stdin(Stdio::inherit())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()?;
        let pid = Pid::from_raw(
            child
                .id()
                .try_into()
                .map_err(|_| CacheError::Configuration)?,
        )
        .ok_or(CacheError::Configuration)?;
        let pidfd = pidfd_open(pid, PidfdFlags::empty())?;
        let mut readiness = [
            PollFd::new(&listener, PollFlags::IN),
            PollFd::new(&pidfd, PollFlags::IN),
        ];
        // Bounded startup admission. A dead guard cannot leave accept blocked.
        poll(
            &mut readiness,
            Some(&Timespec {
                tv_sec: 15,
                tv_nsec: 0,
            }),
        )?;
        if !readiness[0].revents().contains(PollFlags::IN) {
            return Err(CacheError::Provider);
        }
        let (mut status, _) = listener.accept()?;
        // Foreground runtime is intentionally not timed out. EOF on guard death
        // fails the command and leaves the durable pending record protected.
        let mut code = [0; 4];
        status.read_exact(&mut code)?;
        let code = i32::from_le_bytes(code);
        if !(0..=255).contains(&code) {
            return Err(CacheError::Provider);
        }
        std::process::exit(code);
    }

    /// Internal local frontend operation; a pending token authorizes one guard.
    pub fn run_bun_guard(&self, args: &[OsString]) -> Result<()> {
        let [generation, token, separator, rest @ ..] = args else {
            return Err(CacheError::Configuration);
        };
        let generation = generation
            .to_str()
            .filter(|s| hash_name(s, 64))
            .ok_or(CacheError::Configuration)?;
        let token = token
            .to_str()
            .filter(|s| hash_name(s, 64))
            .ok_or(CacheError::Configuration)?;
        if separator != "--" {
            return Err(CacheError::Configuration);
        }
        let mut status = UnixStream::connect_addr(&address(token)?)?;
        let shard = directory(&self.root, &Path::new("bun").join(generation))?;
        let _lease = lease(&shard, false, false)?;
        let runs = directory(&shard, Path::new(".bun-runs"))?;
        if regular(&runs, Path::new(token), 1)? != b"1"
            || regular(&shard, Path::new(".bun-lifetime"), BUN_LIFETIME.len())? != BUN_LIFETIME
        {
            return Err(CacheError::Ownership);
        }
        // prctl expects a nonzero boolean; the safe rustix API represents it
        // as an optional Pid. getpid is nonzero on every supported Linux host.
        set_child_subreaper(Some(rustix::process::getpid()))?;
        let executable = self.config.bun.as_ref().ok_or(CacheError::Provider)?;
        let mut command = self.command(
            executable,
            vec![(
                "BUN_INSTALL_CACHE_DIR".into(),
                self.config
                    .root
                    .join("bun")
                    .join(generation)
                    .join("cache")
                    .into_os_string(),
            )],
            rest,
        );
        let result = command.spawn()?.wait()?;
        use std::os::unix::process::ExitStatusExt;
        let code = result
            .code()
            .unwrap_or_else(|| 128 + result.signal().unwrap_or(1));
        // Close only the guard's copies. Native descendants keep their own stdio.
        let null = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/null")?;
        rustix::stdio::dup2_stdin(&null)?;
        rustix::stdio::dup2_stdout(&null)?;
        rustix::stdio::dup2_stderr(&null)?;
        // An absent caller must not abandon already-started descendants.
        let _ = status.write_all(&code.to_le_bytes());
        drop(status);
        loop {
            match wait(WaitOptions::empty()) {
                Ok(_) => {}
                Err(rustix::io::Errno::INTR) => continue,
                Err(rustix::io::Errno::CHILD) => break,
                Err(error) => return Err(error.into()),
            }
        }
        runs.remove_file(token)?;
        Ok(())
    }
}
