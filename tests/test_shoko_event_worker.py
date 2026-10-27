"""Private event-worker lifecycle; no live Shoko or Seerr writes."""

import threading
import unittest

from nagisa.shoko_event_worker import ShokoEventWorker


class Client:
    def __init__(self, *, end_on_poll=False, fail_connect=False):
        self.gap_detected = True
        self.end_on_poll = end_on_poll
        self.fail_connect = fail_connect
        self.calls = []

    def connect(self):
        self.calls.append('connect')
        if self.fail_connect:
            raise ConnectionError('private test failure')
        self.gap_detected = False

    def poll(self):
        self.calls.append('poll')
        if self.end_on_poll:
            self.gap_detected = True
            return 0
        return 1

    def close(self):
        self.calls.append('close')
        self.gap_detected = True


class Processor:
    def __init__(self):
        self.calls = 0

    def review_one(self):
        self.calls += 1
        return None


class RecordingStop(threading.Event):
    def __init__(self):
        super().__init__()
        self.delays = []

    def wait(self, timeout=None):
        self.delays.append(timeout)
        return len(self.delays) >= 3


class WorkerTests(unittest.TestCase):
    def test_connection_factory_failure_is_visible_in_health(self):
        def fail_factory():
            raise ConnectionError('private test failure')

        worker = ShokoEventWorker(fail_factory, Processor())
        with self.assertRaises(ConnectionError):
            worker.run_connection()
        self.assertEqual(worker.snapshot()['last_error'], 'ConnectionError')
        self.assertFalse(worker.snapshot()['connected'])

    def test_clean_reconnections_keep_the_short_retry_delay(self):
        stop = RecordingStop()
        clients = iter(Client(end_on_poll=True) for _ in range(3))
        worker = ShokoEventWorker(lambda: next(clients), Processor(),
                                  stop=stop, retry_seconds=5)
        worker.run_forever()
        self.assertEqual(stop.delays, [5, 5, 5])
        self.assertEqual(worker.snapshot()['connections'], 3)

    def test_a_successful_connection_resets_failure_backoff(self):
        stop = RecordingStop()
        clients = iter([Client(fail_connect=True),
                        Client(fail_connect=True), Client(end_on_poll=True)])
        worker = ShokoEventWorker(lambda: next(clients), Processor(),
                                  stop=stop, retry_seconds=5)
        worker.run_forever()
        self.assertEqual(stop.delays, [5, 10, 5])
        self.assertEqual(worker.snapshot()['connections'], 1)

    def test_gap_closes_and_next_connection_must_catch_up_again(self):
        first = Client(end_on_poll=True)
        second = Client(end_on_poll=True)
        clients = iter([first, second])
        worker = ShokoEventWorker(lambda: next(clients), Processor())
        worker.run_connection()
        worker.run_connection()
        self.assertEqual(first.calls, ['connect', 'poll', 'close'])
        self.assertEqual(second.calls, ['connect', 'poll', 'close'])
        self.assertEqual(worker.snapshot()['connections'], 2)

    def test_stop_closes_active_connection_before_returning(self):
        stop = threading.Event()
        client = Client()

        def stop_after_one_poll():
            client.calls.append('poll')
            stop.set()
            return 1

        client.poll = stop_after_one_poll
        worker = ShokoEventWorker(lambda: client, Processor(), stop=stop)
        worker.run_connection()
        self.assertEqual(client.calls, ['connect', 'poll', 'close'])
        self.assertTrue(worker.snapshot()['stopped'])

    def test_exact_file_review_is_separate_from_event_receipt(self):
        client = Client(end_on_poll=True)
        processor = Processor()
        worker = ShokoEventWorker(lambda: client, processor)
        worker.run_connection()
        self.assertEqual(processor.calls, 0)
        worker.review_pending_once()
        self.assertEqual(processor.calls, 1)

    def test_bounded_review_budget_drains_a_background_slice(self):
        class QueuedProcessor(Processor):
            def __init__(self):
                super().__init__()
                self.remaining = list(range(1, 26))

            def review_one(self):
                self.calls += 1
                return self.remaining.pop(0) if self.remaining else None

        processor = QueuedProcessor()
        worker = ShokoEventWorker(lambda: Client(), processor,
                                  review_limit=5)
        self.assertEqual(worker.review_pending_once(), 1)
        self.assertEqual(len(processor.remaining), 20)
        for _ in range(4):
            worker.review_pending_once()
        self.assertEqual(processor.remaining, [])
        self.assertEqual(processor.calls, 25)
        with self.assertRaisesRegex(ValueError, 'review'):
            ShokoEventWorker(lambda: Client(), processor, review_limit=0)

    def test_backlog_drain_runs_before_one_file_review(self):
        order = []

        class Catchup:
            def drain_once(self):
                order.append('drain')
                return 1

            def backlog_count(self):
                return 0

        class OrderedProcessor(Processor):
            def review_one(self):
                order.append('review')
                return super().review_one()

        worker = ShokoEventWorker(lambda: Client(), OrderedProcessor(),
                                  catchup=Catchup())
        worker.review_pending_once()
        self.assertEqual(order, ['drain', 'review'])

    def test_private_request_retry_runs_after_file_review(self):
        order = []
        class OrderedProcessor(Processor):
            def review_one(self):
                order.append('file')
                return 'checked'
        class Retry:
            def run_once(self):
                order.append('request')
                return {}
        worker = ShokoEventWorker(lambda: Client(), OrderedProcessor(),
                                  request_retry=Retry())
        self.assertEqual(worker.review_pending_once(), 'checked')
        self.assertEqual(order, ['file', 'request'])

    def test_native_mapping_audit_runs_in_the_existing_review_worker(self):
        order = []

        class OrderedProcessor(Processor):
            def review_one(self):
                order.append('file')
                return 'checked'

        class MappingAudit:
            def run_once(self):
                order.append('mapping')

        worker = ShokoEventWorker(lambda: Client(), OrderedProcessor(),
                                  mapping_audit=MappingAudit())
        self.assertEqual(worker.review_pending_once(), 'checked')
        self.assertEqual(order, ['file', 'mapping'])

    def test_scoped_series_recheck_queues_after_file_review(self):
        order = []

        class OrderedProcessor(Processor):
            def review_one(self):
                order.append('file')
                return 'checked'

        class SeriesRecheck:
            def run_once(self):
                order.append('series')

        worker = ShokoEventWorker(lambda: Client(), OrderedProcessor(),
                                  series_recheck=SeriesRecheck())
        self.assertEqual(worker.review_pending_once(), 'checked')
        self.assertEqual(order, ['file', 'series'])

    def test_request_catchup_still_runs_during_file_backlog(self):
        order = []
        class OrderedProcessor(Processor):
            result = 'checked'
            def review_one(self):
                order.append('file')
                return self.result
        class Catchup:
            def run_once(self):
                order.append('catchup')
        processor = OrderedProcessor()
        worker = ShokoEventWorker(lambda: Client(), processor,
                                  request_catchup=Catchup())
        worker.review_pending_once()
        self.assertEqual(order, ['file', 'catchup'])
        processor.result = None
        worker.review_pending_once()
        self.assertEqual(order, ['file', 'catchup', 'file', 'catchup'])

    def test_health_exposes_undrained_gap_backlog(self):
        class Catchup:
            def drain_once(self):
                return 0

            def backlog_count(self):
                return 501

        worker = ShokoEventWorker(lambda: Client(), Processor(),
                                  catchup=Catchup())
        self.assertEqual(worker.snapshot()['backlog_file_ids'], 501)

    def test_stop_unblocks_an_outstanding_poll(self):
        entered = threading.Event()
        released = threading.Event()
        client = Client()

        def blocking_poll():
            entered.set()
            if not released.wait(2):
                raise AssertionError('poll was not released')
            client.gap_detected = True
            return 0

        def close():
            client.calls.append('close')
            released.set()
            client.gap_detected = True

        client.poll = blocking_poll
        client.close = close
        worker = ShokoEventWorker(lambda: client, Processor())
        thread = threading.Thread(target=worker.run_connection)
        thread.start()
        self.assertTrue(entered.wait(1))
        worker.stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_close_error_does_not_abort_shutdown(self):
        client = Client()
        worker = ShokoEventWorker(lambda: client, Processor())
        worker._active = client

        def fail_close():
            raise ConnectionError('private close failure')

        client.close = fail_close
        worker.stop()
        self.assertTrue(worker.snapshot()['stopped'])
        self.assertEqual(worker.snapshot()['last_error'], 'ConnectionError')

    def test_failed_connection_is_closed_and_does_not_claim_health(self):
        client = Client(fail_connect=True)
        worker = ShokoEventWorker(lambda: client, Processor())
        with self.assertRaises(ConnectionError):
            worker.run_connection()
        self.assertEqual(client.calls, ['connect', 'close'])
        self.assertFalse(worker.snapshot()['connected'])


if __name__ == '__main__':
    unittest.main()
