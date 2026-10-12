"""Independent bounded memory background worker; never alters collector health."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from .bridge import ArchiveBridge
from .embeddings import index_once
from .events import BudgetExceededError
from .extraction import ModelCallError, llm_candidates, rule_candidates, source_rejection
from .sync import MemorySync


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class MemoryWorker:
    def __init__(self, store, archive_db, sync_root_callable=None, interval=30):
        self.store, self.archive_db = store, archive_db
        self.sync_root_callable = sync_root_callable
        self.interval = max(1, float(interval))
        self.stop_event, self.wake = Event(), Event()
        self.lock, self.run_lock = Lock(), Lock()
        self.thread: Thread | None = None
        self.started = time.monotonic()
        self.bridge = ArchiveBridge(store, archive_db)
        self.state = {'state': 'waiting', 'running': False, 'last_attempt': None,
                      'last_success': None, 'error': None, 'cycles': 0,
                      'bridge': {}, 'sync': {}, 'processing': {}, 'index': {}}

    def _set(self, **values) -> None:
        with self.lock:
            self.state.update(values)

    def snapshot(self) -> dict:
        with self.lock:
            result = dict(self.state)
        result.update(managed=self.thread is not None, interval_seconds=self.interval,
                      uptime_seconds=int(time.monotonic() - self.started))
        return result

    def start(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = Thread(target=self._loop, name='awb-memory', daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        # In-flight bounded calls can finish; no endless UI shutdown waits.
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)

    def request(self) -> dict:
        self.wake.set()
        return self.snapshot()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake.clear()
            self.run_once()
            self.wake.wait(self.interval)

    def run_once(self) -> dict:
        if not self.run_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            settings = self.store.settings(private=True)
            status = self.store.status()
            if not settings.get('enabled'):
                self._set(state='disabled', running=False, error=None)
                return self.snapshot()
            if not status.get('space_id'):
                self._set(state='setup_required', running=False, error=None)
                return self.snapshot()
            # Restores without a joining credential are deliberately read-only.
            if status.get('has_join_key') is False or status.get('processing_allowed') is False:
                self._set(state='credential_required', running=False, error=None)
                return self.snapshot()
            self._set(state='processing', running=True, last_attempt=_now(), error=None)
            sync_root = settings.get('sync_root')
            if not sync_root and self.sync_root_callable:
                sync_root = self.sync_root_callable()
            sync = {'exported': 0, 'imported': 0, 'errors': []}
            if sync_root:
                try:
                    sync = MemorySync(self.store, sync_root).run_once()
                except Exception:
                    sync = {'exported': 0, 'imported': 0, 'errors': ['memory_sync_failed']}
            # Even when the share/model is down, bridge and local rule mode work.
            bridge = self.bridge.run_once(limit=20)
            processing = {'claimed': 0, 'completed': 0, 'candidates': 0, 'errors': 0, 'deferred': 0}
            route = settings.get('llm', {})
            calls = 0
            for source in self.store.claim_jobs(limit=2):
                processing['claimed'] += 1
                identifier, token = source['id'], source['lease_token']
                try:
                    if source_rejection(source) or not self.store.source_available(identifier):
                        self.store.finish_job(identifier, token)
                        processing['completed'] += 1
                        continue
                    candidates = rule_candidates(source)
                    if route.get('enabled') and not self.stop_event.is_set():
                        calls += 1
                        candidates.extend(llm_candidates(self.store, source, route))
                    unique = {}
                    for candidate in candidates:
                        key = (candidate['text'], candidate['kind'], candidate['evidence'][0]['quote'])
                        unique.setdefault(key, candidate)
                    # This single core transaction verifies checksum, token, lease,
                    # source suppression and immutable scope before committing.
                    if not self.stop_event.is_set() and self.store.settings().get('enabled'):
                        committed = self.store.propose_from_job(identifier, token, list(unique.values())[:8])
                        processing['candidates'] += len(committed)
                    else:
                        self.store.finish_job(identifier, token, error='worker_stopped', retry=True)
                        continue
                    self.store.finish_job(identifier, token)
                    processing['completed'] += 1
                except BudgetExceededError:
                    # Budget exhaustion is not a provider/source failure. Keep
                    # rule candidates and preserve finite failure attempts so
                    # tomorrow's request window can make progress.
                    if not self.stop_event.is_set() and self.store.settings().get('enabled'):
                        committed = self.store.propose_from_job(identifier, token, rule_candidates(source))
                        processing['candidates'] += len(committed)
                    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                        hour=0, minute=0, second=5, microsecond=0).timestamp()
                    self.store.defer_job(identifier, token, next_run=tomorrow)
                    processing['deferred'] += 1
                except Exception as exc:
                    error = str(exc) if isinstance(exc, ModelCallError) else 'memory_processing_failed'
                    # Provider outages should still leave deterministic rule
                    # candidates reviewable. Core deduplication makes retries safe.
                    if not self.stop_event.is_set() and self.store.settings().get('enabled'):
                        try:
                            committed = self.store.propose_from_job(identifier, token, rule_candidates(source))
                            processing['candidates'] += len(committed)
                        except (ValueError, LookupError):
                            pass  # stale lease or suppression is authoritative
                    self.store.finish_job(identifier, token, error=error, retry=True)
                    processing['errors'] += 1
            index = (index_once(self.store, limit=max(0, 2 - calls))
                     if not self.stop_event.is_set() and self.store.settings().get('enabled') else {})
            self._set(state='warning' if processing['errors'] or sync.get('errors') else 'waiting',
                      running=False, bridge=bridge, sync=sync, processing=processing, index=index,
                      last_success=_now(), cycles=self.snapshot()['cycles'] + 1)
            # Newly created operations can wait for next bounded sync pass; the UI
            # exposes pending export rather than implying immediate NAS delivery.
            return self.snapshot()
        except Exception:
            self._set(state='error', running=False, error='memory_worker_failed')
            return self.snapshot()
        finally:
            self.run_lock.release()
