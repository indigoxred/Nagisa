"""Nagisa-owned lifecycle for the specifically approved arrival duty.

Construction does not poll or dispatch. The caller supplies the composed loop,
stock Sonarr client and a dedicated thread-capable SQLite connection; this service
owns that connection after construction. No separate process or mapper is added.
"""
import threading,time
from nagisa.worklist import import_item

_STATUSES=frozenset({'pending','waiting-identity','blocked-identity','blocked-source','ambiguous','waiting-visibility','blocked-preservation','settling-visibility','awaiting-visibility','delivered','unqualified-owner','waiting-relay','blocked-build','held-backlog'})

class ArrivalCompatibilityError(RuntimeError):
    """Configured stock Sonarr contract is unavailable or unsupported."""

class ArrivalService:
    def __init__(self,sonarr,loop,*,poll_seconds=15,provider='Sonarr'):
        if type(poll_seconds) is not int or not 15<=poll_seconds<=300:
            raise ValueError('arrival poll interval must be 15 to 300 seconds')
        self.sonarr=sonarr;self.loop=loop;self.poll_seconds=poll_seconds
        self.provider=provider
        self._cycle=threading.Lock();self._status_lock=threading.Lock()
        self._closed=False;self._last_error=None;self._last_cycle=None;self._counts={}
        self._refresh_counts()

    def _refresh_counts(self):
        counts={}
        for status,count in self.loop.db.execute('SELECT status,COUNT(*) FROM arrival_inbox GROUP BY status'):
            label=status if status in _STATUSES else 'unknown'
            counts[label]=counts.get(label,0)+count
        has_reasons=self.loop.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='arrival_failure_reasons'").fetchone() is not None
        query=("SELECT i.history_id,i.payload,i.status,r.reason FROM arrival_inbox i LEFT JOIN arrival_failure_reasons r ON r.history_id=i.history_id WHERE i.status!='delivered' ORDER BY i.history_id LIMIT 100"
               if has_reasons else "SELECT history_id,payload,status,NULL FROM arrival_inbox WHERE status!='delivered' ORDER BY history_id LIMIT 100")
        items=[import_item(identity,payload,state,_STATUSES,reason=reason) for identity,payload,state,reason in self.loop.db.execute(query)]
        with self._status_lock:self._counts=counts;self._work_items=items

    def run_once(self):
        if not self._cycle.acquire(blocking=False):return False
        try:
            if self._closed:return False
            try:
                # Recheck before history reads and durable cursor changes. A
                # background application update must not bypass the existing
                # configured reader contract; recovery needs no journal reset.
                compatible,_version,_reason=self.sonarr.compatibility()
                if compatible is not True:
                    raise ArrivalCompatibilityError(self.provider+' compatibility check failed')
                self.loop.poll_once(self.sonarr)
            except Exception as error:
                with self._status_lock:
                    self._last_error=type(error).__name__;self._last_cycle=time.time()
                raise
            else:
                self._refresh_counts()
                with self._status_lock:
                    self._last_error=None;self._last_cycle=time.time()
                return True
        finally:self._cycle.release()

    def status_snapshot(self):
        # Cached public summary: no SQLite access or long network wait on WebUI.
        with self._status_lock:
            return {'state':'stopped' if self._closed else 'enabled','poll_seconds':self.poll_seconds,
                    'last_cycle':self._last_cycle,'last_error':self._last_error,'counts':dict(self._counts),
                    'work_items':[dict(item) for item in self._work_items]}

    def close(self):
        # Wait for a bounded in-flight cycle before closing its dedicated journal.
        with self._cycle:
            if self._closed:return
            self.loop.db.close()
            with self._status_lock:self._closed=True
