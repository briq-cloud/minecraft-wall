import copy
import json
import tempfile
import threading
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
            row = {**self.rows["bq_worker"], "id": "bq_fork", "name": body["name"], "state": "creating", "costCentsSoFar": 0}
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
        self.tmp.cleanup()

    def start_sync(self, count=1):
        with patch('controller.threading.Thread'):
            self.fleet.start(count)
        self.fleet.launch(self.fleet.state['run'])

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
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')

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
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')
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
        self.assertFalse(any(c[0] == 'POST' for c in self.api.calls))

    def test_adopts_existing_exec_then_preserves_final_cost_and_exit(self):
        self.start_sync()
        self.fleet.tick()
        self.assertEqual(self.fleet.state['workers']['bq_worker']['execId'], 'ex_game')
        self.assertFalse(any(c[0] == 'POST' for c in self.api.calls))
        self.api.exec.update(state='exited', exitCode=0)
        self.api.rows['bq_worker']['costCentsSoFar'] = 12.125
        self.fleet.tick()
        self.fleet.tick()
        snapshot = self.fleet.snapshot()
        self.assertEqual(snapshot['summary']['finished'], 1)
        self.assertEqual(snapshot['summary']['costCents'], 2.125)
        self.assertEqual(snapshot['run']['finishedAt'], 1000)
        self.assertNotIn('host', snapshot['workers']['bq_worker'])
        self.assertEqual(json.loads(self.path.read_text())['run']['finishedAt'], 1000)

    def test_stop_during_fork_captures_late_id_then_stops_after_creation(self):
        self.api.fork_release = threading.Event()
        self.fleet.start(2)
        self.assertTrue(self.api.fork_entered.wait(1))
        self.fleet.stop()
        self.api.fork_release.set()
        self.fleet.launch_thread.join(2)
        self.assertFalse(self.fleet.launching)
        self.fleet.tick()
        self.assertFalse(any(c[1] == '/v1/briqs/bq_fork/stop' for c in self.api.calls))
        self.api.rows['bq_fork']['state'] = 'running'
        self.fleet.tick()
        self.assertTrue(any(c[1] == '/v1/briqs/bq_fork/stop' for c in self.api.calls))
        self.assertFalse(any(c[1] == '/v1/briqs/bq_template/stop' for c in self.api.calls))
        self.assertEqual(set(self.fleet.state['workers']), {'bq_worker', 'bq_fork'})

    def test_deadline_stops_worker_and_nonzero_exit_is_error(self):
        self.start_sync()
        self.fleet.tick()
        self.api.exec.update(state='exited', exitCode=137)
        self.fleet.tick()
        self.fleet.tick()
        self.assertEqual(self.fleet.snapshot()['summary']['errors'], 1)
        self.assertEqual(self.fleet.state['workers']['bq_worker']['exitCode'], 137)
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')

    def test_deadline_automatically_requests_stop(self):
        self.start_sync()
        self.fleet.clock = lambda: 2000
        self.fleet.tick()
        self.assertTrue(self.fleet.state['run']['cancelled'])
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')

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
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')

    def test_definite_stop_refusal_can_recover(self):
        self.start_sync()
        self.fleet.tick()
        original = self.api.call
        attempts = []
        def reject_once(method, path, body=None):
            if path.endswith('/stop'):
                attempts.append(path)
                if len(attempts) == 1:
                    raise APIRejected('API HTTP 429')
            return original(method, path, body)
        self.api.call = reject_once
        self.fleet.stop()
        self.fleet.tick()
        self.fleet.tick()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.api.rows['bq_worker']['state'], 'stopped')

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
