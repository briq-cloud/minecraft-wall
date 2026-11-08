"""Deploy the private wall once from a prepared, stopped template. No automatic retries."""
import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

from controller import API


def write_receipt(path, value, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(json.dumps(value, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        return
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def deploy(api, template_id, stack, receipt_path=Path('.minecraft-wall/deploy.json')):
    if not re.fullmatch(r'bq_[A-Za-z0-9]+', template_id):
        raise ValueError('Invalid template ID')
    quota = api.call('GET', '/v1/quota')
    team_id = quota.get('team', {}).get('id')
    if not team_id:
        raise ValueError('Quota must report the credential team')
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if any(receipt.get(key) != value for key, value in {
            'apiUrl': api.base, 'templateId': template_id, 'stack': stack, 'teamId': team_id
        }.items()):
            raise ValueError('Existing deployment receipt belongs to a different target')
        if not receipt.get('controllerId'):
            raise ValueError('Unresolved deployment intent: reconcile its unique name in the dashboard; do not retry creation')
        row = api.call('GET', '/v1/briqs/' + receipt['controllerId'])['briq']
        if row['teamId'] != team_id:
            raise ValueError('Controller team mismatch')
        return row
    template = api.call('GET', '/v1/briqs/' + template_id)['briq']
    stacks = api.call('GET', '/v1/stacks')['stacks']
    matching = [row for row in stacks if row['name'] == stack or row['id'] == stack]
    if (template['teamId'] != team_id or len(matching) != 1 or
            template['stackId'] != matching[0]['id']):
        raise ValueError('Template must belong to this team and the requested stack')
    if template['state'] != 'stopped' or not template.get('volume'):
        raise ValueError('Prepare a template with a persistent volume, then stop it before deployment')
    if 'BRIQ_API_KEY' in template.get('env', {}):
        raise ValueError('The template must not contain the controller API key')
    if not quota.get('features', {}).get('literalEnvSecrets'):
        raise ValueError('This API must support literal environment secrets')
    if ('nano' not in quota.get('availableSizes', []) or quota['remainingConcurrency'] < 1 or
            quota['maxDiskGb'] - quota['diskGb'] < 1 or
            quota['maxMemoryGb'] - quota['memoryGb'] < .5):
        raise ValueError('Controller requires one available nano slot, 512 MB memory and 1 GB disk')
    if min(quota['creditBalanceCents'], quota['remainingTodayCents']) <= 0:
        raise ValueError('A positive balance and daily allowance are required')
    sizes = api.call('GET', '/v1/pricing')['sizes']
    config = {'expectedTeamId': team_id, 'templateId': template_id, 'stack': matching[0]['name'],
              'workerMemoryGb': sizes[template['size']]['memoryMb'] / 1024,
              'workerDiskGb': template['volume']['gb'], 'workers': []}
    root = Path(__file__).parent
    files = {f'/app/{name}': (root / name).read_text() for name in ('controller.py', 'controller.html')}
    files['/app/config.json'] = json.dumps(config)
    identity = uuid.uuid4().hex
    name = 'minecraft-wall-' + identity[:10]
    payload = {'stack': matching[0]['name'], 'expose': 'private', 'wait': False, 'spec': {
        'name': name, 'image': 'python:3.12-slim', 'size': 'nano',
        'volume': {'gb': 1, 'mount': '/work'}, 'ports': [8090], 'expose': 8090,
        'sleep': '1h', 'cmd': ['python', '/app/controller.py'], 'files': files,
        'env': {'BRIQ_API_URL': api.base, 'MINECRAFT_CONFIG': '/app/config.json',
                'MINECRAFT_STATE': '/work/state.json',
                'BRIQ_API_KEY': {'secret': 'MINECRAFT_CONTROLLER_API_KEY', 'value': api.key, 'literal': True}}
    }}
    receipt = {'apiUrl': api.base, 'teamId': team_id, 'templateId': template_id,
               'stack': stack, 'name': name, 'idempotencyKey': identity}
    # The receipt contains no credential or private endpoint URL. Never replay an unknown create.
    write_receipt(receipt_path, receipt, exclusive=True)
    result = api.call('POST', '/v1/briqs', payload, idempotency_key=identity)
    row = result['briqs'][0]
    receipt['controllerId'] = row['id']
    write_receipt(receipt_path, receipt)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True)
    parser.add_argument('--stack', default='minecraft-wall')
    args = parser.parse_args()
    try:
        row = deploy(API(), args.template, args.stack)
        print(f"Controller {row['id']}: {row['state']}")
        print('Open its private URL from the dashboard: https://app.briq.cloud/briqs/' + row['id'])
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(str(exc) if not isinstance(exc, KeyError) else 'Required environment or API field missing', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
