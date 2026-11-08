import copy
import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from controller import APIRejected, Fleet, GAME_CMD, make_handler


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.rows = {"bq_worker": {"id": "bq_worker", "name": "client-02", "state": "running", "internalHost": "worker.internal", "costCentsSoFar": 10}}
        self.exec = {"id": "ex_game", "state": "running", "cmd": GAME_CMD, "exitCode": None}
        self.quota = {"team": {"id": "tm_test"}, "remainingConcurrency": 9, "creditBalanceCents": 1000, "remainingTodayCents": 200, "spentTodayCents": 25, "memoryGb": 2, "maxMemoryGb": 20, "diskGb": 16, "maxDiskGb": 100}
        self.fork_entered = threading.Event()
        self.fork_release = None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v1/quota":
            return copy.deepcopy(self.quota)
        if path.endswith('/fork'):
            self.fork_entered.set()
            if self.fork_release:
                self.fork_release.wait(2)
            identity = "bq_fork" if "bq_fork" not in self.rows else "bq_fork" + str(len(self.rows))
            row = {**self.rows["bq_worker"], "id": identity, "name": body["name"], "state": "creating", "costCentsSoFar": 0}
            self.rows[row["id"]] = row
            return {"briqs": [copy.deepcopy(row)]}
        parts = path.split('/')
        row = self.rows[parts[3]]
        if method == "GET" and len(parts) == 4:
            return {"briq": copy.deepcopy(row)}
        if method == "GET" and path.endswith('/execs'):
            return {"execs": [copy.deepcopy(self.exec)]}
        if method == "GET" and '/execs/' in path:
            return {"exec": copy.deepcopy(self.exec)}
        if method == "DELETE" and len(parts) == 4:
            row["state"] = "destroyed"
            return {"briq": copy.deepcopy(row)}
        if method == "DELETE" and '/execs/' in path:
            self.exec.update(state='stopped', exitCode=None)
            return {"exec": copy.deepcopy(self.exec)}
        if method == "POST" and path.endswith('/start'):
            row['state'] = 'starting'
            return {"briq": copy.deepcopy(row)}
        if method == "POST" and path.endswith('/stop'):
            row['state'] = 'stopped'
            return {"briq": copy.deepcopy(row)}
        raise AssertionError((method, path, body))


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.config = {"templateId": "bq_template", "expectedTeamId": "tm_test", "stack": "minecraft-prototype", "workerMemoryGb": 2, "workerDiskGb": 8, "workers": [{"id": "bq_worker", "name": "client-02"}]}
        self.path = Path(self.tmp.name) / 'state.json'
        self.fleet = Fleet(self.api, self.config, self.path, clock=lambda: 1000)

    def tearDown(self):
        self.fleet.cleanup.shutdown(wait=True)
        self.tmp.cleanup()

    def drain(self):
        for job in list(self.fleet.destroy_jobs.values()):
            job.result(timeout=2)

    def start_sync(self, count=1):
        with patch('controller.threading.Thread'):
            self.fleet.start(count)
        self.fleet.launch(self.fleet.state['run'])
        for identity in self.fleet.state['run']['workers']:
            self.api.rows[identity]['state'] = 'running'

    def test_add_preserves_run_deadline_game_and_cost_baseline(self):
        self.start_sync()
        self.fleet.tick()
        original = copy.deepcopy(self.fleet.state['run'])
        self.api.rows['bq_fork']['costCentsSoFar'] = 2
        self.fleet.clock = lambda: 1100
        with patch('controller.threading.Thread'):
            self.fleet.start(1, duration=1800, mode='cold', add=True)
        self.fleet.launch(self.fleet.state['run'])
        run = self.fleet.state['run']
        self.assertEqual(run['id'], original['id'])
        self.assertEqual(run['deadline'], original['deadline'])
        self.assertEqual(run['requested'], 2)
        self.assertEqual(self.fleet.state['workers']['bq_fork']['execId'], 'ex_game')
        self.assertEqual(self.fleet.state['workers']['bq_fork']['baseline'], 0)
        self.assertEqual(self.fleet.snapshot()['summary']['costCents'], 2)
        self.assertEqual(self.fleet.snapshot()['summary']['preparing'], 2)

    def test_fork_mode_always_creates_fresh_workers_from_ready_worker(self):
        self.start_sync()
        self.fleet.state['workers']['bq_fork']['phase'] = 'running'
        with patch('controller.threading.Thread'):
            self.fleet.start(1, mode='fork')
        self.fleet.launch(self.fleet.state['run'])
        forks = [path for method, path, _ in self.api.calls if path.endswith('/fork')]
        self.assertEqual(forks, ['/v1/briqs/bq_template/fork', '/v1/briqs/bq_fork/fork'])
        self.assertEqual(self.fleet.state['run']['batches'][-1]['sourceId'], 'bq_fork')
        self.assertEqual(self.fleet.state['workers']['bq_fork']['baseline'], 0)

    def test_fork_without_game_ready_source_uses_template_even_with_idle_worker(self):
        with patch('controller.threading.Thread'):
            self.fleet.start(1, mode='fork')
        self.fleet.launch(self.fleet.state['run'])
        self.assertEqual(self.fleet.state['run']['workers'], ['bq_fork'])
        self.assertTrue(any(path == '/v1/briqs/bq_template/fork' for _, path, _ in self.api.calls))

    def test_cold_mode_forks_template_instead_of_reusing_stopped_worker(self):
        self.api.rows['bq_worker']['state'] = 'stopped'
        self.start_sync()
        self.assertFalse(any(path.endswith('/start') for _, path, _ in self.api.calls))
        self.assertEqual(self.fleet.state['run']['workers'], ['bq_fork'])
        self.assertTrue(any(path == '/v1/briqs/bq_template/fork' for _, path, _ in self.api.calls))

    def test_add_during_inflight_fork_reserves_and_receipts_all_workers(self):
        self.api.fork_release = threading.Event()
        self.fleet.start(2)
        self.assertTrue(self.api.fork_entered.wait(1))
        self.fleet.start(1, mode='fork', add=True)
        self.assertEqual(self.fleet.snapshot()['summary']['preparing'], 3)
        self.assertEqual(self.fleet.state['run']['requested'], 3)
        self.api.fork_release.set()
        self.fleet.launch_thread.join(2)
        self.assertFalse(self.fleet.launching)
        self.assertEqual(len(self.fleet.state['run']['workers']), 3)
        self.assertNotIn('forkIntent', self.fleet.state)

    def test_stop_during_add_preflight_cancels_before_mutation(self):
        self.start_sync()
        original = self.api.call
        def stop_on_read(method, path, body=None):
            result = original(method, path, body)
            if path == '/v1/briqs/bq_fork' and method == 'GET':
                self.fleet.stop()
            return result
        self.api.call = stop_on_read
        with self.assertRaisesRegex(ValueError, 'cancelled|Quota'):
            self.fleet.start(1, add=True)
        self.assertEqual(self.fleet.state['run']['requested'], 1)
        self.assertEqual(sum(path.endswith('/fork') for _, path, _ in self.api.calls), 1)

    def test_stop_during_add_fork_captures_late_worker(self):
        self.start_sync()
        self.api.fork_entered.clear()
        self.api.fork_release = threading.Event()
        self.fleet.start(1, mode='fork', add=True)
        self.assertTrue(self.api.fork_entered.wait(1))
        intent = json.loads(self.path.read_text())['forkIntent']
        self.assertEqual(intent['sourceId'], 'bq_template')
        self.assertEqual(intent['copies'], 'disk')
        self.fleet.stop()
        self.api.fork_release.set()
        self.fleet.launch_thread.join(2)
        self.drain()
        self.assertEqual(self.api.rows['bq_fork2']['state'], 'destroyed')

    def test_additional_quota_excludes_active_workers_and_requires_fork_disk(self):
        self.start_sync()
        self.api.quota.update(remainingConcurrency=0)
        self.fleet.refresh_quota(force=True)
        quota = self.fleet.snapshot()['quota']
        self.assertEqual(quota['additionalCapacity'], 0)
        self.assertEqual(quota['forkCapacity'], 0)
        self.assertEqual(quota['capacity'], 1)
        with self.assertRaisesRegex(ValueError, 'Quota'):
            self.fleet.start(1)
        self.api.quota.update(remainingConcurrency=9, maxDiskGb=16)
        with self.assertRaisesRegex(ValueError, 'disk'):
            self.fleet.start(1, mode='fork')

    def test_fleet_and_owned_disk_caps_are_global(self):
        self.start_sync()
        with self.assertRaisesRegex(ValueError, '100'):
            self.fleet.start(100)
        for number in range(99):
            identity = 'bq_extra' + str(number)
            self.fleet.state['workers'][identity] = {'id': identity, 'phase': 'stopped', 'vmState': 'stopped'}
            self.api.rows[identity] = {**self.api.rows['bq_fork'], 'id': identity, 'state': 'stopped'}
        with self.assertRaisesRegex(ValueError, 'Quota'):
            self.fleet.start(1, mode='fork')

    def test_summary_distinguishes_vm_ready_game_ready_and_pending_forks(self):
        with patch('controller.threading.Thread'):
            self.fleet.start(3)
        summary = self.fleet.snapshot()['summary']
        self.assertEqual(summary['vmRunning'], 0)
        self.assertEqual(summary['running'], 0)
        self.assertEqual(summary['preparing'], 3)
        self.fleet.state['workers']['bq_selected'] = {'id': 'bq_selected', 'phase': 'running', 'vmState': 'running'}
        self.fleet.state['run']['workers'].append('bq_selected')
        summary = self.fleet.snapshot()['summary']
        self.assertEqual(summary['running'], 1)
        self.assertEqual(summary['preparing'], 2)
        self.fleet.stop()
        self.assertEqual(self.fleet.snapshot()['summary']['preparing'], 0)

    def test_finished_run_during_add_preflight_is_not_revived(self):
        self.start_sync()
        original = self.api.call
        def finish_on_read(method, path, body=None):
            result = original(method, path, body)
            if path == '/v1/briqs/bq_fork' and method == 'GET':
                self.fleet.state['run']['finishedAt'] = 1000
            return result
        self.api.call = finish_on_read
        with self.assertRaisesRegex(ValueError, 'finished during'):
            self.fleet.start(1, add=True)
        self.assertEqual(self.fleet.state['run']['requested'], 1)
        self.assertEqual(sum(path.endswith('/fork') for _, path, _ in self.api.calls), 1)

    def test_snapshot_immediately_reserves_pending_launch_capacity(self):
        self.api.quota['remainingConcurrency'] = 3
        with patch('controller.threading.Thread'):
            self.fleet.start(3)
        quota = self.fleet.snapshot()['quota']
        self.assertEqual(quota['additionalCapacity'], 0)
        self.assertEqual(quota['forkCapacity'], 0)
        self.assertEqual(quota['capacity'], 3)

    def test_zero_daily_budget_blocks_start_before_any_worker_read(self):
        self.api.quota['remainingTodayCents'] = 0
        with self.assertRaisesRegex(ValueError, 'credit budget'):
            self.fleet.start(1)
        self.assertEqual([call[1] for call in self.api.calls], ['/v1/quota'])
        self.assertEqual(self.fleet.snapshot()['quota']['budgetCents'], 0)
        self.assertFalse(self.fleet.snapshot()['quota']['canStart'])

    def test_credit_snapshot_uses_lower_limit_and_expires(self):
        self.fleet.refresh_quota()
        q = self.fleet.snapshot()['quota']
        self.assertEqual(q['budgetCents'], 200)
        self.assertEqual(q['creditBalanceCents'], 1000)
        self.assertEqual(q['spentTodayCents'], 25)
        self.assertTrue(q['fresh'])
        self.assertTrue(q['canStart'])
        self.fleet.clock = lambda: 1031
        self.assertFalse(self.fleet.snapshot()['quota']['canStart'])
        self.assertFalse(self.fleet.snapshot()['quota']['fresh'])
        self.api.quota['creditBalanceCents'] = 50
        self.fleet.refresh_quota()
        self.assertEqual(self.fleet.snapshot()['quota']['budgetCents'], 50)

    def test_budget_exhaustion_cancels_and_stops_active_run(self):
        self.start_sync()
        self.api.quota['remainingTodayCents'] = 0
        self.fleet.refresh_quota(force=True)
        self.assertTrue(self.fleet.state['run']['cancelled'])
        self.assertIn('exhausted', self.fleet.state['run']['stopReason'])
        self.fleet.tick()
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')

    def test_outage_blocks_start_but_allows_stop_after_verified_identity(self):
        self.start_sync()
        original = self.api.call
        def outage(method, path, body=None):
            if path == '/v1/quota':
                raise RuntimeError('Unavailable')
            return original(method, path, body)
        self.api.call = outage
        self.fleet.refresh_quota(force=True)
        self.assertFalse(self.fleet.snapshot()['quota']['canStart'])
        self.fleet.stop()
        self.fleet.tick()
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')
        self.fleet.tick()
        with self.assertRaisesRegex(ValueError, 'Quota unavailable'):
            self.fleet.start(1)

    def test_unverified_or_wrong_team_never_reads_worker_during_tick(self):
        self.api.quota['team']['id'] = 'tm_other'
        self.fleet.tick()
        self.assertEqual([call[1] for call in self.api.calls], ['/v1/quota'])
        self.assertFalse(self.fleet.snapshot()['quota']['canStart'])

    def test_idle_quota_refresh_is_throttled_to_ten_seconds(self):
        self.fleet.refresh_quota()
        self.fleet.refresh_quota()
        self.assertEqual(len(self.api.calls), 1)
        self.fleet.clock = lambda: 1010
        self.fleet.refresh_quota()
        self.assertEqual(len(self.api.calls), 2)

    def test_team_identity_precedes_resource_reads(self):
        self.api.quota['team']['id'] = 'tm_other'
        with self.assertRaisesRegex(ValueError, 'team'):
            self.fleet.start(1)
        self.assertEqual(len(self.api.calls), 1)

    def test_quota_and_disk_reject_before_mutation(self):
        self.api.quota['remainingConcurrency'] = 0
        with self.assertRaisesRegex(ValueError, 'Quota'):
            self.fleet.start(2)
        self.api.quota['remainingConcurrency'] = 9
        self.api.quota['maxDiskGb'] = 16
        with self.assertRaisesRegex(ValueError, 'disk'):
            self.fleet.start(2)
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/exec') for c in self.api.calls))

    def test_adopts_existing_exec_then_preserves_final_cost_and_exit(self):
        self.start_sync()
        self.fleet.tick()
        self.assertEqual(self.fleet.state['workers']['bq_fork']['execId'], 'ex_game')
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/exec') for c in self.api.calls))
        self.api.exec.update(state='exited', exitCode=0)
        self.api.rows['bq_fork']['costCentsSoFar'] = 2.125
        self.fleet.tick()
        self.drain()
        self.fleet.tick()
        snapshot = self.fleet.snapshot()
        self.assertEqual(snapshot['summary']['finished'], 1)
        self.assertEqual(snapshot['summary']['costCents'], 2.125)
        self.assertEqual(snapshot['run']['finishedAt'], 1000)
        self.assertNotIn('host', snapshot['workers']['bq_fork'])
        self.assertEqual(json.loads(self.path.read_text())['run']['finishedAt'], 1000)

    def test_stop_during_fork_destroys_late_creating_worker(self):
        self.api.fork_release = threading.Event()
        self.fleet.start(2)
        self.assertTrue(self.api.fork_entered.wait(1))
        self.fleet.stop()
        self.api.fork_release.set()
        self.fleet.launch_thread.join(2)
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')
        self.assertFalse(any(c[1].startswith('/v1/briqs/bq_template') and c[0] == 'DELETE' for c in self.api.calls))
        self.assertEqual(set(self.fleet.state['workers']), {'bq_worker', 'bq_fork'})

    def test_deadline_stops_worker_and_nonzero_exit_is_error(self):
        self.start_sync()
        self.fleet.tick()
        self.api.exec.update(state='exited', exitCode=137)
        self.fleet.tick()
        self.drain()
        self.fleet.tick()
        self.assertEqual(self.fleet.snapshot()['summary']['errors'], 1)
        self.assertEqual(self.fleet.state['workers']['bq_fork']['exitCode'], 137)
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')

    def test_deadline_automatically_requests_stop(self):
        self.start_sync()
        self.fleet.clock = lambda: 2000
        self.fleet.tick()
        self.assertTrue(self.fleet.state['run']['cancelled'])
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')

    def test_http_controls_require_same_origin_json_and_header(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.fleet))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host = '127.0.0.1:' + str(server.server_port)
            connection = HTTPConnection(host)
            for headers in [{}, {'Origin': 'https://other.example', 'Content-Type': 'application/json', 'X-Minecraft-Control': '1'}]:
                connection.request('POST', '/api/stop', '{}', headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            connection.request('POST', '/api/stop', '{}', {'Origin': 'http://' + host, 'Content-Type': 'application/json', 'X-Minecraft-Control': '1'})
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            # briq's edge overwrites forwarding headers and sends an internal Host.
            connection.request('POST', '/api/stop', '{}', {'Origin': 'https://wall.example', 'X-Forwarded-Host': 'wall.example', 'X-Forwarded-Proto': 'https', 'Content-Type': 'application/json', 'X-Minecraft-Control': '1'})
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_unknown_fork_is_not_replayed_and_blocks_next_run(self):
        original = self.api.call
        def fail(method, path, body=None):
            if path.endswith('/fork'):
                raise RuntimeError('API outcome unknown; mutation was not retried')
            return original(method, path, body)
        self.api.call = fail
        self.start_sync(2)
        self.assertIn('forkIntent', self.fleet.state)
        self.fleet.tick()
        self.drain()
        self.fleet.tick()
        self.assertIsNone(self.fleet.state['run']['finishedAt'])
        with self.assertRaisesRegex(ValueError, 'Reconcile|Stop and finish'):
            self.fleet.start(1)
        self.assertIsNone(self.fleet.state['run']['finishedAt'])
        self.assertIn('forkIntent', Fleet(self.api, self.config, self.path).state)

    def test_stop_does_not_depend_on_exec_observation(self):
        self.start_sync()
        self.fleet.tick()
        original = self.api.call
        def fail(method, path, body=None):
            if '/execs/' in path:
                raise RuntimeError('Exec observation unavailable')
            return original(method, path, body)
        self.api.call = fail
        self.fleet.stop()
        self.fleet.tick()
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')

    def test_definite_stop_refusal_can_recover(self):
        self.start_sync()
        self.fleet.tick()
        original = self.api.call
        attempts = []
        def reject_once(method, path, body=None):
            if method == 'DELETE' and path == '/v1/briqs/bq_fork':
                attempts.append(path)
                if len(attempts) == 1:
                    raise APIRejected('API HTTP 429')
            return original(method, path, body)
        self.api.call = reject_once
        self.fleet.stop()
        self.fleet.tick()
        self.drain()
        self.fleet.tick()
        self.assertEqual(len(attempts), 2)
        self.drain()
        self.assertEqual(self.api.rows['bq_fork']['state'], 'destroyed')

    def test_rate_limit_is_status_delay_not_game_failure(self):
        self.start_sync()
        self.fleet.tick()
        original = self.api.call
        def rate_limit(method, path, body=None):
            if method == 'GET' and '/execs/' in path:
                raise APIRejected('API HTTP 429')
            return original(method, path, body)
        self.api.call = rate_limit
        self.fleet.tick()
        self.assertEqual(self.fleet.snapshot()['summary']['errors'], 0)
        self.assertIn('warning', self.fleet.state['workers']['bq_fork'])
        self.api.call = original
        self.fleet.tick()
        self.assertNotIn('warning', self.fleet.state['workers']['bq_fork'])

    def test_exec_checks_are_paced_and_rotate(self):
        self.start_sync()
        self.fleet.tick()
        base = copy.deepcopy(self.fleet.state['workers']['bq_fork'])
        for number in range(5):
            identity = 'bq_extra' + str(number)
            self.fleet.state['workers'][identity] = {**base, 'id': identity}
            self.api.rows[identity] = {**self.api.rows['bq_fork'], 'id': identity}
        seen = set()
        for number in range(3):
            self.fleet.clock = lambda n=number: 1001 + n
            self.api.calls.clear()
            self.fleet.tick()
            checks = [path for method, path, _ in self.api.calls if method == 'GET' and '/execs/' in path]
            self.assertLessEqual(len(checks), 2)
            seen.update(path.split('/')[3] for path in checks)
        self.assertEqual(len(seen), 6)

    def test_stop_uses_only_vm_delete_without_snapshot_or_exec_stop(self):
        self.start_sync()
        self.fleet.tick()
        self.fleet.stop()
        self.drain()
        actions = [(method, path) for method, path, _ in self.api.calls]
        self.assertIn(('DELETE', '/v1/briqs/bq_fork'), actions)
        self.assertFalse(any(path.endswith('/stop') or '/execs/' in path and method == 'DELETE' for method, path in actions))

    def test_stop_dispatches_bounded_parallel_cleanup_and_deduplicates(self):
        self.fleet.refresh_quota()
        release = threading.Event()
        entered = threading.Event()
        counter_lock = threading.Lock()
        inflight = 0
        peak = 0
        started = []
        for number in range(19):
            identity = 'bq_extra' + str(number)
            self.fleet.state['workers'][identity] = {'id': identity, 'phase': 'ready', 'vmState': ['creating', 'stopped', 'error'][number % 3]}
            self.api.rows[identity] = {**self.api.rows['bq_worker'], 'id': identity, 'state': self.fleet.state['workers'][identity]['vmState']}
        original = self.api.call
        def slow_delete(method, path, body=None):
            nonlocal inflight, peak
            if method == 'DELETE':
                with counter_lock:
                    inflight += 1
                    peak = max(peak, inflight)
                    started.append(path)
                    if inflight == 8:
                        entered.set()
                release.wait(2)
                try:
                    return original(method, path, body)
                finally:
                    with counter_lock:
                        inflight -= 1
            return original(method, path, body)
        self.api.call = slow_delete
        try:
            before = time.monotonic()
            self.fleet.stop()
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertTrue(entered.wait(1))
            self.fleet.stop()
            self.assertEqual(len(started), 8)
        finally:
            release.set()
            self.drain()
        self.assertEqual(peak, 8)
        self.assertEqual(len(started), 20)
        self.assertEqual(len(set(started)), 20)
        self.assertTrue(all(w['vmState'] == 'destroyed' and w['phase'] == 'stopped' for w in self.fleet.state['workers'].values()))

    def test_unknown_destroy_is_never_replayed_and_get_reconciles(self):
        self.start_sync()
        original = self.api.call
        attempts = []
        def unknown_delete(method, path, body=None):
            if method == 'DELETE' and path == '/v1/briqs/bq_fork':
                attempts.append(path)
                raise RuntimeError('Connection lost')
            return original(method, path, body)
        self.api.call = unknown_delete
        self.fleet.stop()
        self.drain()
        worker = self.fleet.state['workers']['bq_fork']
        self.assertEqual(worker['intent'], 'destroy')
        self.fleet.stop()
        self.fleet.tick()
        self.drain()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(worker['intent'], 'destroy')
        self.api.rows['bq_fork'].update(state='destroyed', costCentsSoFar=3.75)
        self.fleet.tick()
        self.assertNotIn('intent', worker)
        self.assertEqual(worker['phase'], 'stopped')
        self.assertEqual(self.fleet.snapshot()['summary']['costCents'], 3.75)
        self.assertEqual(self.fleet.state['run']['finishedAt'], 1000)

    def test_destroyed_history_does_not_consume_capacity_or_get_read(self):
        for number in range(150):
            identity = 'bq_history' + str(number)
            self.fleet.state['workers'][identity] = {'id': identity, 'phase': 'stopped', 'vmState': 'destroyed', 'cost': 2}
        self.start_sync()
        self.assertEqual(self.fleet.state['run']['workers'], ['bq_fork'])
        self.assertFalse(any('/bq_history' in path for _, path, _ in self.api.calls))
        self.assertEqual(len(self.fleet.state['workers']), 152)

    def test_unknown_start_does_not_prevent_permanent_cleanup(self):
        self.fleet.refresh_quota()
        self.fleet.state['workers']['bq_worker'].update(intent='start', vmState='stopped')
        self.fleet.stop()
        self.drain()
        self.assertEqual(self.api.rows['bq_worker']['state'], 'destroyed')
        self.assertNotIn('intent', self.fleet.state['workers']['bq_worker'])

    def test_stop_during_health_read_preserves_destroyed_phase(self):
        self.start_sync()
        self.fleet.tick()
        def stop_then_health(*_):
            self.fleet.stop()
            self.drain()
            return json.dumps({'gameLoaded': True, 'javaAlive': True, 'frameAgeSeconds': 0}).encode()
        with patch('controller.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.side_effect = stop_then_health
            self.fleet.tick()
        self.assertEqual(self.fleet.state['workers']['bq_fork']['phase'], 'stopped')
        self.assertEqual(self.fleet.snapshot()['summary']['running'], 0)

    def test_destroyed_legacy_stop_error_does_not_block_fresh_launch(self):
        self.fleet.state['workers']['bq_worker'].update(vmState='destroyed', gameStopError='old unresolved exec stop', intent='stop')
        self.start_sync()
        self.assertEqual(self.fleet.state['run']['workers'], ['bq_fork'])
        self.fleet.stop()
        self.drain()
        self.fleet.tick()
        self.assertIsNotNone(self.fleet.state['run']['finishedAt'])
        self.assertNotIn('intent', self.fleet.state['workers']['bq_worker'])
        self.start_sync()
        self.assertIsNone(self.fleet.state['run']['finishedAt'])

    def test_stale_frames_are_not_counted_running(self):
        self.start_sync()
        self.fleet.tick()
        with patch('controller.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'gameLoaded': True, 'javaAlive': True, 'frameAgeSeconds': 60}).encode()
            self.fleet.tick()
        self.assertEqual(self.fleet.snapshot()['summary']['running'], 0)


if __name__ == '__main__':
    unittest.main()
