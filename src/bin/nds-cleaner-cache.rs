fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() == 2 && args[1] == "--version" {
        println!("nds-cleaner-cache {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    run();
}
// Local operator entry point; no remote command or agent capability is added.
#[cfg(any(target_os = "linux", target_os = "macos"))]
fn run() {
    use nddev_device_sync_cleaner::cache::{ManagedCache, read_config};
    let result = (|| {
        let mut args = std::env::args_os().skip(1);
        let config = args
            .next()
            .ok_or("usage: nds-cleaner-cache CONFIG init|cargo|uv|bun|clean [--apply]")?;
        let operation = args.next().ok_or("missing operation")?;
        let args: Vec<_> = args.collect();
        let config_path = std::path::PathBuf::from(config);
        let config = read_config(&config_path).map_err(|e| e.to_string())?;
        if operation == "init" && args.is_empty() {
            ManagedCache::initialize(config).map_err(|e| e.to_string())?;
            println!("{{\"initialized\":true}}");
            return Ok(());
        }
        let cache = ManagedCache::open(config).map_err(|e| e.to_string())?;
        match operation.to_str() {
            Some("cargo") => cache.run_cargo(&args),
            Some("uv" | "bun") => {
                cache.run_tool(&config_path, operation.to_str().unwrap_or_default(), &args)
            }
            #[cfg(target_os = "linux")]
            Some("bun-guard") => cache.run_bun_guard(&args),
            Some("clean") => {
                let (apply, passes, batch) = match args.as_slice() {
                    [] => (false, 1, false),
                    [apply] if apply == "--apply" => (true, 1, false),
                    [apply, flag, count] if apply == "--apply" && flag == "--passes" => {
                        let count = count
                            .to_str()
                            .and_then(|s| s.parse::<usize>().ok())
                            .filter(|n| (1..=32).contains(n))
                            .ok_or("passes must be between 1 and 32")?;
                        (true, count, true)
                    }
                    _ => return Err("unsupported cleanup arguments".to_owned()),
                };
                let mut reports = Vec::new();
                for _ in 0..passes {
                    let report = cache.collect(apply).map_err(|e| e.to_string())?;
                    let complete = report.scan_complete;
                    reports.push(report);
                    if complete {
                        break;
                    }
                }
                let complete = reports.last().is_some_and(|r| r.scan_complete);
                let value = if batch {
                    serde_json::json!({"schema": 1, "scan_complete": complete, "passes": reports})
                } else {
                    serde_json::to_value(&reports[0]).map_err(|_| "report encoding failed")?
                };
                println!(
                    "{}",
                    serde_json::to_string(&value).map_err(|_| "report encoding failed")?
                );
                if batch && !complete {
                    std::process::exit(75);
                }
                return Ok(());
            }
            _ => return Err("unsupported operation".to_owned()),
        }
        .map_err(|e| e.to_string())
    })();
    if let Err(error) = result {
        eprintln!("NDS Cleaner: {error}");
        std::process::exit(1);
    }
}
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn run() {
    eprintln!("NDS managed cache maintenance is qualified for Linux and macOS only");
    std::process::exit(1);
}
