# Changelog

## 0.8.0 — Visible and recoverable Windows background (2026-10-06)

- Add a native Windows tray with collection status, open/refresh/pause/resume/restart/log/exit actions; browser favicon, title and status bar distinguish successful collection, pending work and disconnection. No Electron or local password prompt.
- Register a limited current-user logon task, unlimited execution duration, battery support and one-minute failure recovery. Repeated launches reuse one instance; isolated development and portable launches do not register the installed task. Add Mac LaunchAgent configuration without claiming Mac device validation.
- Separate scheduled collection from archive queries. Archive changed source files incrementally, observe Hermes WAL changes, cache message summaries and filter detail reads by native session. Preserve historical facts when sources disappear.
- Keep the first historical import nonblocking for dashboard queries; isolate loopback health/restart checks from system HTTP proxy settings.
- Exclude imported synthetic task spans from verified execution time while retaining messages and estimated activity; correct long custom-range monthly buckets.
- Refresh model reporting, navigation, responsive layout and shared typography from the existing local UI work. Preserve year/month/week/day heatmaps and honest Hermes aggregate precision.
- Check stable GitHub releases in the background; verify installer size/hash, snapshot program and SQLite archive, coordinate shutdown/install/restart, validate health and attempt rollback on failure. Preserve failure evidence to avoid repeated automatic installs.
- Embed source commit, version and build timestamp in packages and health/status APIs. Update Windows packaging, installer registration/removal, workflows, portable instructions and the usage/validation guide.
- Windows validation results are attached to this release. Reboot/logon, 72-hour endurance, Mac/NAS device exchange and full live remote-update cycles remain explicit untested boundaries.


## 0.7.0 — Model intelligence and session discovery (2026-09-29)

- Replace the redundant usage-period conversation panel with model comparison, share, trend, hourly frequency, request distribution, cache-rate charts and a model detail table. Make chart metrics switchable and keep Hermes session aggregates out of dated/request-level charts.
- Add session sorting by text size, Agent duration, source size, message count and confirmed file count, with keyword scope and minimum-value filters.
- Refresh the dashboard typography, spacing, surfaces and visual hierarchy for desktop and narrow screens.
- Rebalance the session toolbar after visual review: date and Agent controls share space with search and quick sorting; hide infrequent conditions behind a clear expandable control.
- Keep Mac and Windows discovery/archive paths aligned; Mac receives basic build and application checks, while real Windows testing remains pending.

## 0.6.0 — Durable archive and two-computer exchange (2026-09-29)

- Persist discovered Codex, Claude and Hermes conversation text, Token observations, run intervals, source locations and file-operation indexes in Workbench-owned SQLite facts. Historical records remain readable after native Agent logs are removed, once they have been discovered.
- Exchange immutable, device-owned packets through a Syncthing `Sync_AI` folder. Each computer keeps its own local SQLite replica; neither computer opens or writes the other's database over NAS.
- Add All/Mac/Windows computer filtering across usage, time, session search and details. All view deduplicates copied native records by Agent, native ID and fact ID.
- Keep produced files as path-only indexes. Folder actions operate only on the original computer when the folder still exists.
- Add a sync-folder field and archive status on the dashboard. Initial Mac backfill and isolated two-device exchange received basic checks; Windows package and NAS behavior still require Windows device testing.

## 0.5.3 — Session navigation and focused conversation view (2026-09-29)

- Show each Codex/Claude source record's disk size or Hermes session payload estimate in the conversation list, with a direct folder action.
- Open the containing folder for a source log, workspace, or recorded file without opening the file itself. Restrict the action to local desktop sessions and paths present in session evidence.
- Add a focused conversation view that keeps user requests and the last completed Agent response per turn; retain the full message view.
- Use spare detail-header space for user turn count, selected-range Agent time, and confirmed file count. Keep file counts limited to operations supported by source evidence.

## 0.5.2 — Session inspector and local search (unreleased)

- Place the conversation list and selected session detail in side-by-side panes on desktop, with a list-to-detail flow on narrow screens.
- Add debounced local search across titles, indexed conversation text, and recorded file/tool paths within the selected date range and Agent.
- Show source record locations, source-reported working directory, disk size, and chronological file operations. Distinguish confirmed tool results from unverified nested patch paths; never infer per-session RAM or claim that unobserved files do not exist.
- Index Codex and Claude sources incrementally, inspect Hermes file-tool results on demand, and keep all indexed text and paths on the local device.

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

## 0.1.0-preview.1 — Windows portable preview

- Provide a Windows portable ZIP started with `Start-AgentWorkbench.cmd`, local owner password setup, and a browser page at `127.0.0.1:8765`.
- Include Codex/Hermes collection instructions in the portable package. Startup, readiness, web assets and login passed a Windows smoke test; Mac/NAS and dual-machine continuation were not validated.
