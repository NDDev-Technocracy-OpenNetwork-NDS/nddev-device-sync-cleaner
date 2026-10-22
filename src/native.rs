use crate::view::*;
use nddev_device_sync_adapter_io::{CancellationToken, IoError, ProcessIo, ProcessRequest};

use std::{path::PathBuf, time::Duration};

#[derive(Clone)]
pub struct Cleaner {
    io: ProcessIo,
    executable: PathBuf,
}

impl Cleaner {
    pub fn new(io: ProcessIo, executable: PathBuf) -> Result<Self, IoError> {
        if !executable.is_absolute() {
            return Err(IoError::InvalidConfiguration);
        }
        Ok(Self { io, executable })
    }

    fn request(&self, args: &[&str]) -> ProcessRequest {
        ProcessRequest {
            executable: self.executable.clone(),
            arguments: args.iter().map(Into::into).collect(),
            directory: None,
            timeout: Duration::from_secs(30),
            max_output_bytes: 1_048_576,
        }
    }

    pub async fn diagnose(&self, cancellation: CancellationToken) -> Result<Diagnostics, IoError> {
        let (report, exit): (Diagnostics, _) = self
            .io
            .json(self.request(&["doctor", "--json"]), &[0, 1], cancellation)
            .await?;
        if !valid_owner(&report.tool, &report.version) || !consistent(&report, exit) {
            return Err(IoError::InvalidResponse);
        }
        Ok(report)
    }

    pub async fn preview(
        &self,
        cancellation: CancellationToken,
    ) -> Result<CleanerRunSummary, IoError> {
        self.report(&["run", "--dry-run", "--json"], true, cancellation)
            .await
    }

    pub async fn last_run(
        &self,
        cancellation: CancellationToken,
    ) -> Result<CleanerRunSummary, IoError> {
        self.report(&["status"], false, cancellation).await
    }

    async fn report(
        &self,
        args: &[&str],
        preview: bool,
        cancellation: CancellationToken,
    ) -> Result<CleanerRunSummary, IoError> {
        let (report, _): (CleanerRunSummary, _) =
            self.io.json(self.request(args), &[0], cancellation).await?;
        if !valid_owner(&report.tool, &report.version) || report.dry_run != preview {
            return Err(IoError::InvalidResponse);
        }
        Ok(report)
    }
}

fn valid_owner(tool: &str, version: &str) -> bool {
    tool == "rldyour-cleaner" && !version.is_empty() && version.len() <= 64
}

fn consistent(report: &Diagnostics, exit: i32) -> bool {
    report.checks.len() <= 256
        && report.errors
            == report
                .checks
                .iter()
                .filter(|check| check.state == CheckState::Error)
                .count()
        && report.warnings
            == report
                .checks
                .iter()
                .filter(|check| check.state == CheckState::Warning)
                .count()
        && exit == i32::from(report.errors > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nddev_device_sync_adapter_io::NativeIo;

    fn check(state: CheckState) -> Check {
        Check {
            id: "check".into(),
            state,
            detail: "detail".into(),
        }
    }

    fn diagnostics(checks: Vec<Check>, errors: usize, warnings: usize) -> Diagnostics {
        Diagnostics {
            tool: "rldyour-cleaner".into(),
            version: "1.0.0".into(),
            checked_unix: 1,
            errors,
            warnings,
            checks,
        }
    }

    #[test]
    fn owner_identity_requires_the_native_tool_and_a_bounded_version() {
        assert!(valid_owner("rldyour-cleaner", "1.0.0"));
        assert!(!valid_owner("other-tool", "1.0.0"));
        assert!(!valid_owner("rldyour-cleaner", ""));
        assert!(!valid_owner("rldyour-cleaner", &"x".repeat(65)));
    }

    #[test]
    fn consistent_report_counts_match_check_states_and_exit() {
        let report = diagnostics(
            vec![
                check(CheckState::Error),
                check(CheckState::Warning),
                check(CheckState::Ok),
            ],
            1,
            1,
        );
        assert!(consistent(&report, 1));
        assert!(!consistent(&report, 0));
        let mut wrong = diagnostics(report.checks.clone(), 2, 1);
        assert!(!consistent(&wrong, 1));
        wrong.errors = 1;
        wrong.warnings = 0;
        assert!(!consistent(&wrong, 1));
    }

    #[test]
    fn clean_report_accepts_zero_exit() {
        let report = diagnostics(vec![check(CheckState::Ok)], 0, 0);
        assert!(consistent(&report, 0));
        assert!(!consistent(&report, 1));
    }

    #[test]
    fn oversized_check_inventory_is_rejected() {
        let report = diagnostics(vec![check(CheckState::Info); 257], 0, 0);
        assert!(!consistent(&report, 0));
    }

    #[test]
    fn constructor_requires_an_absolute_executable() {
        let io = NativeIo::new(1).unwrap().process_handle();
        assert!(matches!(
            Cleaner::new(io.clone(), PathBuf::from("rldyour-cleaner")),
            Err(IoError::InvalidConfiguration)
        ));
        assert!(Cleaner::new(io, std::env::temp_dir().join("rldyour-cleaner")).is_ok());
    }

    #[test]
    fn requests_are_bounded_in_time_and_output() {
        let io = NativeIo::new(1).unwrap().process_handle();
        let executable = std::env::temp_dir().join("rldyour-cleaner");
        let cleaner = Cleaner::new(io, executable.clone()).unwrap();
        let request = cleaner.request(&["doctor", "--json"]);
        assert_eq!(request.executable, executable);
        assert_eq!(request.arguments, ["doctor", "--json"]);
        assert_eq!(request.timeout, Duration::from_secs(30));
        assert_eq!(request.max_output_bytes, 1_048_576);
        assert!(request.directory.is_none());
    }
}
