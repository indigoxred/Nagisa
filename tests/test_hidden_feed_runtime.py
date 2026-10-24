"""One private file feed participates in Nagisa's normal lifecycle."""

import unittest
from unittest.mock import patch

from nagisa.runtime import NagisaRuntime


class Web:
    is_alive = False

    def start(self):
        self.is_alive = True

    def stop(self):
        self.is_alive = False


class Feed:
    def __init__(self):
        self.reviewed = 0
        self.checked = 0
        self.stopped = False
        self.connected = True

    def review_pending_once(self):
        self.reviewed += 1

    def check_recent_once(self):
        self.checked += 1

    def run_forever(self):
        pass

    def stop(self):
        self.stopped = True

    def snapshot(self):
        return {'connected': self.connected, 'connections': 1,
                'last_error': None, 'stopped': self.stopped,
                'backlog_file_ids': 0}


class HiddenFeedRuntimeTests(unittest.TestCase):
    def make(self, worker=None):
        return NagisaRuntime.for_tests(
            ordering=object(), reconciler=object(), web=Web(),
            file_event_worker=worker)

    def test_opt_in_worker_ticks_and_health_are_visible(self):
        worker = Feed()
        runtime = self.make(worker)
        runtime.tick_file_event_review()
        runtime.tick_file_event_catchup()
        self.assertEqual((worker.reviewed, worker.checked), (1, 1))
        runtime.start(start_threads=False)
        self.assertTrue(runtime.health_snapshot()['healthy'])
        worker.connected = False
        self.assertFalse(runtime.health_snapshot()['healthy'])
        self.assertIsNone(self.make().file_event_worker)

    def test_worker_connection_stops_before_thread_join(self):
        events = []
        worker = Feed()
        worker.stop = lambda: events.append('stop-feed')

        class Thread:
            def __init__(self, *, target, name, daemon, args=()):
                self.name = name

            def start(self):
                events.append('start-' + self.name)

            def join(self, *, timeout):
                events.append('join-' + self.name)

        runtime = self.make(worker)
        with patch('nagisa.runtime.threading.Thread', Thread):
            runtime.start()
            self.assertIn('start-nagisa-file-event-connection', events)
            runtime.stop()
        self.assertLess(events.index('stop-feed'),
                        events.index('join-nagisa-file-event-connection'))
