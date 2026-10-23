"""Independent ordering, reconciliation and WebUI runtime boundaries."""

from __future__ import annotations

import json
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nagisa.audit import AuditLog
from nagisa import __version__
from nagisa.config import Config
from nagisa.http import JsonTransport
from nagisa.ordering.evidence import EvidenceArchive
from nagisa.ordering.plex import PlexObserver as OrderingPlexObserver
from nagisa.ordering.service import OrderingService
from nagisa.ordering.shoko import ShokoClient
from nagisa.reconciler.service import ReconcilerService, Trigger
from nagisa.reconciler.future_guard import FutureEpisodeGuard
from nagisa.reconciler.sonarr import SonarrClient
from nagisa.state import SCHEMA_VERSION, StateStore
from nagisa.web.server import NagisaWebApp, NagisaWebServer


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _deployment_status(config_dir: Path) -> dict[str, Any]:
    path = Path(config_dir) / "deployment.json"
    unavailable = {"available": False}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            return unavailable
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return unavailable
        result: dict[str, Any] = {"available": True}
        for key in ("image", "previous_image", "installed_at", "backup_id"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                return unavailable
            if any(ord(character) < 32 for character in value):
                return unavailable
            result[key] = value.strip()
        return result
    except (OSError, UnicodeError, ValueError, TypeError):
        return unavailable


class _NullAudit:
    def rows(self):
        return []


class _NullFutureGuard:
    def run_once(self):
        return {}

    def status_snapshot(self):
        return {"mode": "report", "compatible": None, "decisions": []}


def _retained_source_aliases(config, delivery) -> dict[Path, Path]:
    """Derive local mount aliases solely from shared native managed-folder IDs.

    These declarations permit a physical-identity check in the Plex observer;
    they do not establish matching content or change either module's roots.
    """
    configured = getattr(config, 'arrival_config_file', None)
    if configured is None or not delivery or not delivery.get('managed_roots'):
        return {}
    from nagisa.reconciler.arrival_startup import ArrivalSettings, _directory

    directory = _directory(str(config.config_dir))
    path = Path(configured)
    if not path.is_absolute() or not path.is_relative_to(directory):
        raise ValueError('arrival configuration must belong to CONFIG_DIR')
    settings = ArrivalSettings.from_file(path)
    aliases = {}
    for key, value in delivery['managed_roots'].items():
        if not isinstance(key, str) or not key.isdigit() or int(key) <= 0 or str(int(key)) != key:
            raise ValueError('positive native managed-folder ID required')
        native = settings.managed_roots.get(int(key))
        retained = _directory(value)
        if native is None or native == retained:
            continue
        if native in aliases and aliases[native] != retained:
            raise ValueError('conflicting retained source aliases')
        aliases[native] = retained
    return aliases


class NagisaRuntime:
    def __init__(self, config: Config, *, arrival=None, subtitles=None, catchup=None,
                 movie_arrival=None, seerr_app=None) -> None:
        self.arrival = arrival
        self.movie_arrival = movie_arrival
        self.subtitles = subtitles
        self.catchup = catchup
        self.seerr_availability = None
        self._delivery_dispatch_lock = threading.Lock()
        self._seerr_availability_lock = threading.Lock()
        self.config = config
        from nagisa.delivery import DeliverySettingsStore
        self.delivery_settings = None if config.delivery_config_file is None else DeliverySettingsStore(
            config.delivery_config_file, config.config_dir, arrival_configured=config.arrival_config_file is not None)
        delivery = None if self.delivery_settings is None else self.delivery_settings.active
        source_aliases = _retained_source_aliases(config, delivery)
        self.state = StateStore(config.config_dir / "nagisa.sqlite3")
        self.audit = AuditLog(self.state)
        shoko = ShokoClient(
            config.shoko_url, config.shoko_token,
            allowed_versions=config.shoko_allowed_versions,
            timeout=config.request_timeout_seconds,
        )
        ordering_plex = OrderingPlexObserver(
            config.plex_url, config.plex_token, config.plex_section_ids,
            timeout=config.request_timeout_seconds,
        )
        sonarr_transport = JsonTransport(
            config.sonarr_url,
            headers={"X-Api-Key": config.sonarr_token},
            timeout=config.request_timeout_seconds,
        )
        sonarr = SonarrClient(
            sonarr_transport, allowed_versions=config.sonarr_allowed_versions,
            import_root=config.sonarr_import_root,
        )
        self.ordering = OrderingService(
            self.state.ordering, shoko, ordering_plex, sonarr,
            mode=config.ordering_mode, now=_utc_now,
            evidence_archive=EvidenceArchive(config.config_dir / "evidence" / "ordering"),
            audit=self.audit,
        )
        from nagisa.reconciler.evidence import ReconcilerEvidenceCollector

        def event_provider(action_id: int, series_id: int):
            from nagisa.reconciler.models import ImportHistoryEvent
            return tuple(ImportHistoryEvent(
                int(row["history_id"]), int(row["series_id"]), int(row["episode_id"]),
                int(row["sonarr_file_id"]), str(row["download_id"]),
                str(row["imported_path"]), str(row["dropped_path"]), int(row["source_size"]),
            ) for row in self.state.reconciler.event_payloads_for_action(
                action_id, series_id
            ))

        observer = ReconcilerEvidenceCollector.from_config(
            config, sonarr=sonarr, event_provider=event_provider,
            managed_roots=None if delivery is None else {int(key): Path(value) for key,value in delivery['managed_roots'].items()},
            source_mounts=None if delivery is None else {key: Path(value) for key,value in delivery['plex_mounts'].items()},
            source_aliases=source_aliases,
        )
        self.reconciler = ReconcilerService(
            self.state.reconciler, sonarr, observer, mode=config.reconciler_mode,
            audit=self.audit,
        )
        normal_transport = JsonTransport(
            config.normal_sonarr_url,
            headers={"X-Api-Key": config.normal_sonarr_token},
            timeout=config.request_timeout_seconds,
        )
        normal_sonarr = SonarrClient(
            normal_transport, allowed_versions=config.normal_sonarr_allowed_versions,
            import_root=config.sonarr_import_root,
        )
        self.future_guard = FutureEpisodeGuard(
            normal_sonarr, mode=config.future_guard_mode,
            threshold_hours=config.future_guard_hours, audit=self.audit,
        )
        self._ordering_interval = config.ordering_poll_seconds
        self._reconciler_interval = config.reconciler_poll_seconds
        self._future_guard_interval = config.future_guard_poll_seconds
        self._owns_state = True
        if getattr(config, "arrival_config_file", None) is not None and (delivery is None or delivery['arrival_enabled']):
            if self.arrival is not None:
                self.state.close()
                raise ValueError("arrival cannot be both injected and configured")
            try:
                from nagisa.reconciler.arrival_startup import build_arrival_service
                self.arrival = build_arrival_service(config, sonarr)
            except BaseException:
                self.state.close()
                raise
        try:
            if delivery is not None and delivery['movie_arrival_enabled']:
                if self.movie_arrival is not None:raise ValueError('movie arrivals cannot be both injected and configured')
                from nagisa.reconciler.arrival_startup import build_arrival_service
                self.movie_arrival=build_arrival_service(config,sonarr,movie=delivery)
                if self.movie_arrival is None:raise ValueError('movie arrivals require reviewed preservation configuration')
            from nagisa.delivery import build_subtitle_service
            if self.subtitles is not None and delivery is not None and delivery['subtitles_enabled']:
                raise ValueError('subtitles cannot be both injected and configured')
            if self.subtitles is None:
                self.subtitles = build_subtitle_service(config, delivery, dispatch_lock=self._delivery_dispatch_lock)
            if delivery is not None and delivery['catchup_enabled']:
                if self.catchup is not None:
                    raise ValueError('catchup cannot be both injected and configured')
                from nagisa.catchup_native import NativeCatchupAdapter
                from nagisa.catchup import build_catchup_service
                adapter = NativeCatchupAdapter(sonarr_transport, allowed_versions=config.sonarr_allowed_versions)
                self.catchup = build_catchup_service(
                    config.config_dir, adapter, binding=adapter.binding_summary(),
                    poll_seconds=delivery['catchup_poll_seconds'],
                    include_backlog=delivery['catchup_include_backlog'],
                    maximum_per_cycle=1, actor_lock=self._delivery_dispatch_lock)
            if config.seerr_guard_config_file is not None:
                if seerr_app is not None:
                    raise ValueError('Seerr guard cannot be both injected and configured')
                from nagisa.seerr_startup import build_seerr_app
                seerr_app = build_seerr_app(config, config.seerr_guard_config_file, self.audit)
            if seerr_app is not None:
                self.seerr_availability = getattr(seerr_app, 'availability_sweep', None)
            app = NagisaWebApp(self, shoko_origin=config.shoko_url,
                               seerr_app=seerr_app)
            self.web = NagisaWebServer(app, config.bind_host, config.port)
        except BaseException:
            if self.movie_arrival is not None:
                self.movie_arrival.close()
            if self.arrival is not None:
                self.arrival.close()
            if self.subtitles is not None:
                self.subtitles.close()
            if self.catchup is not None:
                self.catchup.close()
            self.state.close()
            raise
        self._initialize_runtime()

    @classmethod
    def for_tests(cls, *, ordering: Any, reconciler: Any, web: Any,
                  future_guard: Any | None = None, arrival=None, subtitles=None, catchup=None, delivery_settings=None, movie_arrival=None) -> "NagisaRuntime":
        instance = cls.__new__(cls)
        instance.arrival = arrival
        instance.movie_arrival = movie_arrival
        instance.subtitles = subtitles
        instance.catchup = catchup
        instance.seerr_availability = None
        instance._delivery_dispatch_lock = threading.Lock()
        instance._seerr_availability_lock = threading.Lock()
        instance.delivery_settings = delivery_settings
        instance.config = None
        instance.state = None
        instance.audit = _NullAudit()
        instance.ordering = ordering
        instance.reconciler = reconciler
        instance.future_guard = future_guard or _NullFutureGuard()
        instance.web = web
        instance._ordering_interval = 15
        instance._reconciler_interval = 900
        instance._future_guard_interval = 15
        instance._owns_state = False
        instance._initialize_runtime()
        return instance

    def _initialize_runtime(self) -> None:
        self._movie_arrival_interval=self.movie_arrival.poll_seconds if self.movie_arrival is not None else None
        self._arrival_interval = self.arrival.poll_seconds if self.arrival is not None else None
        if self.delivery_settings is not None:
            self._arrival_interval = self.delivery_settings.active['arrival_poll_seconds']
        self._subtitle_interval = self.subtitles.poll_seconds if self.subtitles is not None else None
        self._catchup_interval = self.catchup.poll_seconds if self.catchup is not None else None
        self._seerr_availability_interval = (
            self.seerr_availability.poll_seconds
            if self.seerr_availability is not None else None)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stopped = False
        self.module_health = {
            "ordering": {"ok": True, "error": None, "last_cycle": None},
            "reconciler": {"ok": True, "error": None, "last_cycle": None},
            "future_guard": {"ok": True, "error": None, "last_cycle": None},
        }

        if self.arrival is not None:
            self.module_health["arrival"] = {"ok": True, "error": None, "last_cycle": None}
        if self.movie_arrival is not None:
            self.module_health['movie_arrival'] = {'ok':True,'error':None,'last_cycle':None}
        if self.subtitles is not None:
            self.module_health['subtitles'] = {'ok':True, 'error':None, 'last_cycle':None}
        if self.catchup is not None:
            self.module_health['catchup'] = {'ok':True, 'error':None, 'last_cycle':None}
        if self.seerr_availability is not None:
            self.module_health['seerr_availability'] = {
                'ok': True, 'error': None, 'last_cycle': None}

    def tick_seerr_availability(self) -> None:
        if self.seerr_availability is None:
            return
        try:
            # Catalogue reads must never hold the arrival dispatch lock:
            # one slow Seerr/Shoko/Plex sweep could delay a new import.
            with self._seerr_availability_lock:
                self.seerr_availability.run_once()
        except Exception as error:
            self._record_health('seerr_availability', RuntimeError(type(error).__name__))
        else:
            self._record_health('seerr_availability', None)

    def seerr_availability_snapshot(self) -> dict:
        return ({'state': 'disabled'} if self.seerr_availability is None
                else self.seerr_availability.status_snapshot())

    def tick_catchup(self) -> None:
        if self.catchup is None:
            return
        try:
            self.catchup.run_once()
        except Exception as error:
            self._record_health('catchup', RuntimeError(type(error).__name__))
        else:
            self._record_health('catchup', None)

    def catchup_snapshot(self) -> dict:
        return {'enabled':False, 'state':'disabled'} if self.catchup is None else self.catchup.status_snapshot()

    def tick_subtitles(self) -> None:
        if self.subtitles is None:
            return
        try:
            self.subtitles.run_once()
        except Exception as error:
            self._record_health('subtitles', RuntimeError(type(error).__name__))
        else:
            self._record_health('subtitles', None)

    def subtitle_snapshot(self) -> dict:
        return {'state':'disabled'} if self.subtitles is None else self.subtitles.status_snapshot()

    def delivery_snapshot(self) -> dict:
        if self.delivery_settings is None:
            return {'configured':False, 'editable':False, 'restart_required':False}
        result = self.delivery_settings.snapshot()
        key = getattr(self.config, 'delivery_settings_token', None)
        result['editable'] = isinstance(key, str) and len(key) >= 32
        return result

    def save_delivery_controls(self, controls, revision, access_key, bindings=None) -> None:
        import secrets
        expected = getattr(self.config, 'delivery_settings_token', None)
        if self.delivery_settings is None or not isinstance(expected, str) or len(expected)<32 or not secrets.compare_digest(expected, access_key):
            raise PermissionError('Delivery settings authentication refused')
        self.delivery_settings.save_controls(controls, revision, bindings)

    def tick_arrival(self) -> None:
        if self.arrival is None:
            return
        try:
            with self._delivery_dispatch_lock:
                result = self.arrival.run_once()
        except Exception as error:
            self._record_health("arrival", RuntimeError(type(error).__name__))
        else:
            if result is not False:
                self._record_health("arrival", None)

    def tick_movie_arrival(self) -> None:
        if self.movie_arrival is None:return
        try:
            with self._delivery_dispatch_lock:result=self.movie_arrival.run_once()
        except Exception as error:self._record_health('movie_arrival',RuntimeError(type(error).__name__))
        else:
            if result is not False:self._record_health('movie_arrival',None)

    def movie_arrival_snapshot(self) -> dict:
        return {'state':'disabled'} if self.movie_arrival is None else self.movie_arrival.status_snapshot()

    def arrival_snapshot(self) -> dict:
        return {"state": "disabled"} if self.arrival is None else self.arrival.status_snapshot()

    def _record_health(self, module: str, error: BaseException | None) -> None:
        self.module_health[module] = {
            "ok": error is None,
            "error": None if error is None else str(error),
            "last_cycle": _utc_now(),
        }

    def tick_ordering(self) -> None:
        try:
            self.ordering.poll_once()
        except Exception as error:
            self._record_health("ordering", error)
        else:
            self._record_health("ordering", None)

    def tick_reconciler(self) -> None:
        if self.delivery_settings is not None and not self.delivery_settings.active['reconciliation_enabled']:
            return
        try:
            self.reconciler.run_once(Trigger.SCHEDULED)
        except Exception as error:
            self._record_health("reconciler", error)
        else:
            self._record_health("reconciler", None)

    def tick_future_guard(self) -> None:
        try:
            self.future_guard.run_once()
        except Exception as error:
            self._record_health("future_guard", error)
        else:
            self._record_health("future_guard", None)

    def _loop(self, callback, interval: int) -> None:
        while not self._stop.is_set():
            callback()
            if self._stop.wait(interval):
                break

    def start(self, *, start_threads: bool = True) -> None:
        if self._started:
            return
        self.web.start()
        if start_threads:
            workers = (
                ("ordering", self.tick_ordering, self._ordering_interval),
                ("reconciler", self.tick_reconciler, self._reconciler_interval),
                ("future-guard", self.tick_future_guard, self._future_guard_interval),
            )
            if self.arrival is not None:
                workers += (("arrival", self.tick_arrival, self._arrival_interval),)
            if self.movie_arrival is not None:
                workers += (('movie-arrival',self.tick_movie_arrival,self._movie_arrival_interval),)
            if self.subtitles is not None:
                workers += (('subtitles', self.tick_subtitles, self._subtitle_interval),)
            if self.catchup is not None:
                workers += (('catchup', self.tick_catchup, self._catchup_interval),)
            if self.seerr_availability is not None:
                workers += (('seerr-availability', self.tick_seerr_availability,
                             self._seerr_availability_interval),)
            for name, callback, interval in workers:
                thread = threading.Thread(target=self._loop, args=(callback, interval), name=f"nagisa-{name}", daemon=True)
                thread.start()
                self._threads.append(thread)
        self._started = True

    def stop(self) -> None:
        if self._stopped:
            return
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self.web.stop()
        if self.movie_arrival is not None:
            self.movie_arrival.close()
        if self.arrival is not None:
            self.arrival.close()
        if self.subtitles is not None:
            self.subtitles.close()
        if self.catchup is not None:
            self.catchup.close()
        if self._owns_state:
            self.state.close()
        self._started = False
        self._stopped = True

    def run_ordering_now(self) -> None:
        self.ordering.poll_once()

    def run_reconciler_now(self) -> None:
        if self.delivery_settings is None or self.delivery_settings.active['reconciliation_enabled']:
            self.reconciler.run_once(Trigger.MANUAL)
        self.future_guard.run_once()

    def health_snapshot(self) -> dict:
        database_ok = True if self.state is None else self.state.quick_check() == "ok"
        web_ok = bool(getattr(self.web, "is_alive", False))
        modules_ok = all(row["ok"] for row in self.module_health.values())
        return {
            "healthy": database_ok and web_ok and modules_ok,
            "app_version": __version__,
            "state_schema": SCHEMA_VERSION,
            "database": "ok" if database_ok else "failed",
            "web": web_ok,
            **self.module_health,
        }

    def deployment_snapshot(self) -> dict[str, Any]:
        if self.config is None:
            return {"available": False}
        return _deployment_status(self.config.config_dir)

    def folder_snapshot(self) -> list[dict]:
        from nagisa.folders import folder_inventory, read_managed_folders
        delivery = self.delivery_snapshot().get('active', {})
        web_app = getattr(self.web, 'app', None)
        seerr_app = getattr(web_app, 'seerr_app', None)
        bindings = getattr(seerr_app, 'folder_bindings', ())
        managed = read_managed_folders(
            getattr(self.config, 'folder_inventory_file', None))
        return folder_inventory(self.config, delivery, bindings, managed)

    def run_forever(self) -> None:
        def request_stop(signum, frame):
            del signum, frame
            self._stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        self.start()
        try:
            self._stop.wait()
        finally:
            self.stop()
