# Changelog

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
