# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-28

### Added

- Telegram download manager.
- Google Drive upload pipeline.
- Upload verification.
- Crash recovery.
- Startup validation.
- Startup recovery.
- Graceful shutdown.
- HealthService.
- AdminService.
- TempFileService.
- Docker support.
- Docker Compose deployment.
- CLI health check.
- Structured logging.
- Business audit logging.
- GitHub Actions CI.
- Telegram admin commands:
  - `/health`
  - `/queues`
  - `/failed`
  - `/stats`
  - `/retry_failed`
  - `/cleanup_temp`
  - `/shutdown`
  - `/cancel`

### Changed

- Download architecture migrated to `forward_origin_chat_id` and `forward_origin_message_id`.
- Admin commands refactored through `AdminCommandService`.
- Cooperative cancellation added for downloads and uploads.

### Fixed

- Download reliability improvements.
- Upload recovery edge cases.
- Queue recovery duplication.
- Graceful shutdown race conditions.
- Various production hardening improvements.
