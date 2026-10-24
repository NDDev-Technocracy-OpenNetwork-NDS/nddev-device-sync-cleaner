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
        let config = read_config(std::path::Path::new(&config)).map_err(|e| e.to_string())?;
        if operation == "init" && args.is_empty() {
            ManagedCache::initialize(config).map_err(|e| e.to_string())?;
            println!("{{\"initialized\":true}}");
            return Ok(());
        }
        let cache = ManagedCache::open(config).map_err(|e| e.to_string())?;
        match operation.to_str() {
            Some("cargo") => cache.run_cargo(&args),
            Some("uv" | "bun") => cache.run_tool(operation.to_str().unwrap_or_default(), &args),
            Some("clean") if args.is_empty() || args == ["--apply"] => {
                let report = cache.collect(!args.is_empty()).map_err(|e| e.to_string())?;
                println!(
                    "{}",
                    serde_json::to_string(&report).map_err(|_| "report encoding failed")?
                );
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
