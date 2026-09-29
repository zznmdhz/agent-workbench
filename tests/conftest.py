"""Keep test archive packets away from the user's live NAS share."""

import pytest


@pytest.fixture(autouse=True)
def isolated_archive_exchange(tmp_path, monkeypatch):
    root = tmp_path / 'sync'
    (root / '.stfolder').mkdir(parents=True)
    monkeypatch.setenv('AWB_SYNC_ROOT', str(root))
