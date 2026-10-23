use nddev_device_sync_adapter_io::{CancellationToken, NativeIo};
use nddev_device_sync_cleaner::{Cleaner, ReadCleaner};

#[tokio::test]
#[ignore = "requires NDS_TEST_CLEANER pointing to the real installed cleaner"]
async fn observes_real_policy_and_run_without_starting_maintenance() {
    assert_eq!(
        std::env::var("NDS_ISOLATED_PROVIDER").as_deref(),
        Ok("1"),
        "run just provider-check"
    );
    let executable = std::env::var_os("NDS_TEST_CLEANER").expect("native cleaner required");
    let provider = Cleaner::new(
        NativeIo::new(1).unwrap().process_handle(),
        executable.into(),
    )
    .unwrap();
    let report = provider.diagnose(CancellationToken::new()).await.unwrap();
    assert!(!report.checks.is_empty());
    assert_eq!(report.tool, "rldyour-cleaner");
    let prior = provider.last_run(CancellationToken::new()).await.unwrap();
    let preview = provider.preview(CancellationToken::new()).await.unwrap();
    assert!(preview.dry_run);
    let after = provider.last_run(CancellationToken::new()).await.unwrap();
    assert_eq!(prior.started_unix, after.started_unix);
    assert_eq!(prior.duration_ms, after.duration_ms);
    assert_eq!(prior.failed, after.failed);
}
