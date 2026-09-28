# Changelog

## 0.5.1 — Conversation browsing controls (unreleased)

- Remove the dense per-run timeline from the dashboard while keeping elapsed-time summaries and the time heatmap.
- Add independent single-day/date-range controls and All/Codex/Claude/Hermes switching to the conversation list; open messages within the same selected range.
- Build and verify the macOS test app; keep the Windows build and version paths aligned pending Windows device testing.

## 0.5.0 — Local conversation and elapsed-time review (unreleased)

- Add a Token/time switch to the shared year, month, week and day heatmap.
- Index Codex, Claude and Hermes conversation metadata locally and load original user/assistant text only when opening a session.
- Show a daily conversation timeline and per-Agent cumulative versus overlap-deduplicated elapsed time.
- Use completed Codex task events as verified time; label message-based Codex, Claude and Hermes spans as estimates, excluding unknown or over-six-hour spans.
- Add Mac real-source verification and packaged-app smoke tests; keep Windows build paths updated for v0.5.0 pending Windows device testing.

## 0.4.2 — Mac test build and usage dashboard (unreleased)

- Add Mac application build and local launch alongside the existing Windows packaging path. Discover Codex, Claude and Hermes native data on both systems without writing to those sources.
- Show Codex's native session name when available; add visible Agent switching, year/month/week/day Token heatmaps, custom date ranges, and a clear count of sessions with usage in the selected period.
- Keep Hermes session/model totals separate from date and hour cells, mark future cells, and make long session titles available in full.
- Speed up initial Codex scans by skipping unrelated JSONL records before parsing large message or tool-output bodies.
- Make heatmap grids fill the available width, add month markers and hover details, show populated-cell counts, and clear stale cells while another period is loading.
- Clarify that usage is local to the running computer and document a device-aware export/import design for future Mac/Windows aggregation.
- Complete macOS automated and browser testing. Windows v0.4.2 installer and update regression remain to be run before a release.

## 0.4.1 — Windows automatic update

- Add verified update downloads for installed Windows copies and an installer path that closes the running application before replacing files.
- Keep the published v0.4.1 package on the earlier password flow; the local passwordless page entered the development branch later.

## 0.4.0 — Multi-Agent usage

- Add Claude Code request usage and Hermes session/model summaries alongside Codex, with explicit differences in time precision.
- Add custom date ranges, Agent/model filters, source coverage and Token breakdowns.

## 0.3.0 — Codex usage MVP test build

- Add request-level Codex usage, model and session views and a separate Windows test build.
- Verify the packaged Windows executable against a test database; user installation acceptance remained pending.

## 0.2.4 — Windows activity review

- Rebuild Codex child-agent attribution from each source file's primary identity; isolate cumulative counters per file and show cached input as a subset of input. A staged, backup-first repair command is included for data indexed by older releases. The installer does not silently rewrite an existing database.
- Add scoped historical-text preview and opt-in backfill for retained Codex/Hermes records, with progress, versioned message revisions, redaction/truncation, and deletion tombstones that block local re-import.
- Make sessions identifiable and renameable; keep date/source filters visible, deep-link to a selected message or run, and paginate long run details without blending later runs.
- Add source-backed file evidence from successful paired Codex `apply_patch` operations and an explicit local check of the file's current status, size, and modification time. Historical file size remains unknown unless recorded by the source.
- Clarify coverage, unknown values, token basis, source scan health, process-sample limits, and the distinction between single-day timelines and multi-day trends. Improve keyboard and narrow-screen navigation.
- Keep incremental and Codex cumulative Token evidence in separate cards and matching drill-downs; stop unknown-ended runs from appearing on later dates, and order filtered sessions by activity inside the selected range.
- Show source-recorded file operation time separately from current file checks. Offline identity repair refuses SQLite WAL/SHM sidecars that could replay stale data after a swap.

## 0.2.3 — Dashboard usability and Token evidence

- Fix blue-filled session and timeline rows caused by a broad button selector.
- Add one-day and 7/10/15-day views, linked device/Agent/model/timezone filters, daily trends, device comparison, and bounded timelines.
- Make every main metric card open its definition, evidence coverage, and related sessions.
- Label untitled sessions clearly and identify former title-like directory names as working directories.
- Backfill Codex cumulative input/output counters from read-only source logs, assigning only same-day counter intervals; keep opening balances and cross-day intervals unallocated.

## 0.2.2 — First import visibility

- Refresh the overview and device state every 15 seconds while the workbench is open, so newly imported history appears without a manual reload.
- Show import progress and automatically select the most recent activity date when today has no imported activity.
- Explain unknown request-level token counts when only cumulative counter evidence is available.

## 0.2.1 — Windows first-run usability

- Start-menu and portable launch now run in the background and open the browser without a terminal window.
- First-use owner password setup moves into the web page, with automatic sign-in after creation.
- Add an authenticated “关闭工作台” action to stop the local server and collector.
- Keep a separate console CLI for backup and advanced operations; existing data and passwords remain valid.
- Add a windowless password-reset launcher that preserves devices, sessions and statistics.

## 0.2.0 — Windows single-machine release

- Added a per-user Windows installer and portable ZIP. App data lives outside the installed program directory.
- Starting the app now discovers local Codex and Hermes sources and runs the read-only collector automatically. The default policy stores statistics only.
- Added a local source policy control for future messages, session/message pagination, title/path search and clearer empty states.
- Fixed outbox epoch persistence, terminal-before-start run projection, top-level duration accounting and initial import throughput.
- Verified installation, synthetic end-to-end collection, real Windows stats-only import, and backup/restore.

Mac/NAS deployment and real cross-machine continuation remain outside this Windows release. The installer is not code-signed and does not add login autostart or automatic updates.
