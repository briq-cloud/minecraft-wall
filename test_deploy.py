import json
import tempfile
import unittest
from pathlib import Path

from deploy import deploy


class FakeAPI:
    base = 'https://api.example.test'
    key = 'test-only-secret'

    def __init__(self):
        self.calls = []
        self.unknown = False
        self.template = {'id': 'bq_template', 'state': 'stopped', 'teamId': 'tm_test',
                         'stackId': 'st_test', 'size': 'small', 'volume': {'gb': 8}, 'env': {}}
        self.quota = {'team': {'id': 'tm_test'}, 'features': {'literalEnvSecrets': True},
                      'availableSizes': ['nano', 'small'], 'remainingConcurrency': 10,
                      'maxDiskGb': 100, 'diskGb': 8, 'maxMemoryGb': 20, 'memoryGb': 0,
                      'creditBalanceCents': 100, 'remainingTodayCents': 50}

    def call(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if path == '/v1/quota':
            return self.quota
        if path == '/v1/stacks':
            return {'stacks': [{'id': 'st_test', 'name': 'minecraft-wall'}]}
        if path == '/v1/pricing':
            return {'sizes': {'small': {'memoryMb': 2048}}}
        if path == '/v1/briqs/bq_template':
            return {'briq': self.template}
        if path == '/v1/briqs/bq_controller':
            return {'briq': {'id': 'bq_controller', 'state': 'running', 'teamId': 'tm_test'}}
        if method == 'POST':
            if self.unknown:
                raise RuntimeError('API outcome unknown; mutation was not retried')
            return {'briqs': [{'id': 'bq_controller', 'state': 'creating'}]}
        raise AssertionError(path)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / '.minecraft-wall' / 'deploy.json'
        self.api = FakeAPI()

    def tearDown(self):
        self.temp.cleanup()

    def test_credential_only_in_secret_payload_and_observation_does_not_create(self):
        deploy(self.api, 'bq_template', 'minecraft-wall', self.path)
        create = next(call for call in self.api.calls if call[0] == 'POST')
        payload = create[2]
        self.assertEqual(payload['spec']['env']['BRIQ_API_KEY'], {
            'secret': 'MINECRAFT_CONTROLLER_API_KEY', 'value': self.api.key, 'literal': True})
        config = json.loads(payload['spec']['files']['/app/config.json'])
        self.assertEqual(config['workerMemoryGb'], 2)
        self.assertEqual(config['workers'], [])
        self.assertEqual(config['expectedTeamId'], 'tm_test')
        self.assertEqual(payload['expose'], 'private')
        self.assertNotIn(self.api.key, self.path.read_text())
        self.assertEqual(json.loads(self.path.read_text())['idempotencyKey'], create[3]['idempotency_key'])
        deploy(self.api, 'bq_template', 'minecraft-wall', self.path)
        self.assertEqual(sum(call[0] == 'POST' for call in self.api.calls), 1)

    def test_unknown_create_keeps_intent_and_cannot_replay(self):
        self.api.unknown = True
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            deploy(self.api, 'bq_template', 'minecraft-wall', self.path)
        self.assertTrue(self.path.exists())
        with self.assertRaisesRegex(ValueError, 'Unresolved deployment intent'):
            deploy(self.api, 'bq_template', 'minecraft-wall', self.path)
        self.assertEqual(sum(call[0] == 'POST' for call in self.api.calls), 1)

    def test_wrong_stack_or_zero_budget_never_creates(self):
        with self.assertRaisesRegex(ValueError, 'requested stack'):
            deploy(self.api, 'bq_template', 'other-stack', self.path)
        self.api.quota['remainingTodayCents'] = 0
        with self.assertRaisesRegex(ValueError, 'positive'):
            deploy(self.api, 'bq_template', 'minecraft-wall', self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(any(call[0] == 'POST' for call in self.api.calls))


if __name__ == '__main__':
    unittest.main()
