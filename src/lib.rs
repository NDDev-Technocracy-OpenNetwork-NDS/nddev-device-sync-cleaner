//! Native cleaner observations and opt-in local development-cache maintenance.
//! The cleaner owns maintenance decisions, reports and cache preservation.

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
mod adapter;
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub mod cache;
pub mod view;

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub use adapter::Cleaner;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub use nddev_device_sync_adapter_io::{CancellationToken, IoError, NativeIo, ProcessIo};

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum Error {
    #[error(transparent)]
    Io(#[from] IoError),
    #[error("cleaner protocol response is invalid or unsupported")]
    Protocol,
    #[error("native cleaner provider is not supported on this platform")]
    UnsupportedPlatform,
}

/// Application port: callers depend on typed observations, not a native process.
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub trait ReadCleaner {
    fn diagnose(
        &self,
        cancellation: CancellationToken,
    ) -> impl std::future::Future<Output = Result<view::Diagnostics, Error>> + Send;
    fn preview(
        &self,
        cancellation: CancellationToken,
    ) -> impl std::future::Future<Output = Result<view::CleanerRunSummary, Error>> + Send;
    fn last_run(
        &self,
        cancellation: CancellationToken,
    ) -> impl std::future::Future<Output = Result<view::CleanerRunSummary, Error>> + Send;
}

/// Canonical compiled-in runtime manifest for this integration.
pub const MANIFEST: &str = include_str!("../contracts/module.json");
