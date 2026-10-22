# Hermes profile archive patch — 1.0.0rc2

The default Hermes database was the only native Hermes input scanned by the durable archive. Sessions written to `profiles/<name>/state.db` never entered the archive or its NAS exchange stream, while the existing collector could still report success for the default database.

This patch discovers the default database and first-level profile databases on each collection cycle, honors `HERMES_HOME`, and keeps `HERMES_STATE_DB` as an explicit single-database override. Existing Windows legacy locations keep precedence; an existing home-directory database is supported when the legacy path does not exist.

Each physical database and its WAL has an independent fingerprint, including resolved source identity. Newly added profiles are discovered without a restart. Default session IDs remain unchanged. Profile session IDs use `profile:<encoded-name>:<native-id>`, and titles include the profile name. Messages, session/model aggregates and verified file evidence use that identity consistently through archive export/import.

Each source is read in one SQLite read transaction. A failed profile does not commit partial source facts or advance its checkpoint; healthy sources still archive, and failures contribute to background scan warnings. Source files are opened read-only. Missing sources do not delete previously archived history. Alias paths resolving to the same physical database are not imported twice.

Validation:

- 183 backend tests passed locally, including 12 focused profile regressions; source lint and frontend build passed.
- Profile tests exercise colliding IDs, new profiles after an unchanged default scan, WAL-only updates, empty and broken sources, retry recovery, source overrides, Windows path precedence, file evidence and NAS replay.
- `packaging/verify_hermes_runtime.py` launches an isolated desktop executable against synthetic default/profile databases, checks both sessions through HTTP, checks exact Chinese bodies and verifies synthetic exchange packets. The existing `verify_memory_runtime.py` packaging gate invokes this check alongside frozen MCP/UTF-8 validation, so the unchanged Windows workflow also requires it.
- Native stores, live archive repair and actual NAS completion require separate local verification. Private diagnostics and real conversation data are excluded from the repository and release assets.

Upgrade uses the existing local archive and memory databases, device identity and shared directory. No database schema migration, source-history deletion, memory reset or second copy of the default session history is required. The first scan imports profile history that older releases did not capture. This is a shared-memory RC1 successor and remains a prerelease; it is not an automatic replacement for the stable 0.8 release.
