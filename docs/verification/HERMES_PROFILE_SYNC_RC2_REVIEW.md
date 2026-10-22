# Hermes profile RC2 independent review

Date: 2026-10-22  
Decision: **APPROVE**  
Scope: commit `7c1c28c` plus the reviewed, uncommitted fixes in `archive.py`, `hermes_archive.py` and `test_hermes_profiles.py`.

The patch addresses the missed first-level Hermes profile databases. Default session identities remain unchanged; profile identities are namespaced consistently for messages, usage, metadata and file evidence. Each source has a database/WAL checkpoint and a coherent read transaction. Healthy sources remain available when a profile fails. Immutable exchange packets preserve the captured facts for another local replica.

Two initial P2 findings are closed:

- Device identity rotation now removes the v2 Hermes checkpoint. The regression test confirms default/profile facts are collected under the new writer, retain their old archive history and can be exported.
- Source discovery, path resolution and collection errors are isolated and reported. A circular profile symlink no longer prevents healthy default messages from being archived.

Independent validation:

```text
.venv/bin/python -m pytest tests/test_hermes_profiles.py tests/test_archive_sync.py --basetemp .local/architecture-audit/hermes-profile-20261022/pytest-revision2 -q
21 passed

.venv/bin/python -m ruff check src/awb/archive.py src/awb/hermes_archive.py tests/test_hermes_profiles.py packaging/verify_memory_runtime.py packaging/verify_hermes_runtime.py
All checks passed!
```

The existing packaging gate now invokes the isolated Hermes desktop runtime check. Its GUI executable lookup is consistent with the Mac/Windows package layout, and the unchanged Windows workflow retains that gate. The Hermes check verifies the HTTP session list, exact archived Chinese bodies and synthetic packet export; it uses fixture data rather than live Agent/NAS data.

No remaining blocking code findings within this patch. This review does not assert that the new Mac/Windows frozen builds, real NAS convergence or production deployment have passed; those require their separate runtime/release evidence. The full suite is being run by the implementation agent and is not claimed as independently rerun here. No implementation files were changed by the reviewer.
