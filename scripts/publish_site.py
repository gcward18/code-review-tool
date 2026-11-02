"""Stage the built frontend using CDK outputs; upload only with explicit --publish.

Run after an approved infrastructure deployment with --outputs pointing at the CDK outputs file.
The default is a local staging operation, with no AWS mutations.
"""
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / '.build/site'


def stage_site(outputs_file, stack):
    outputs = json.loads(outputs_file.read_text())[stack]
    config = json.loads(outputs['RuntimeConfig'])
    site_url = outputs['SiteUrl']
    if (config.get('mode') != 'cloud' or config.get('apiBaseUrl') != ''
            or config['cognito']['redirectUri'] != site_url or not site_url.startswith('https://')):
        raise ValueError('Unexpected deployment runtime configuration')
    expected_upload_origin = f"https://{outputs['DocumentBucket']}.s3.{outputs['Region']}.amazonaws.com"
    if config.get('uploadOrigin') != expected_upload_origin:
        raise ValueError('Deploy direct-upload infrastructure before publishing this frontend')
    source = ROOT / 'autosde/static' 
    if not (source / 'index.html').is_file() or not (source / 'assets').is_dir():
        raise ValueError('Build the frontend first')
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    shutil.copytree(source / 'assets', STAGING / 'assets')
    shutil.copyfile(source / 'index.html', STAGING / 'index.html')
    (STAGING / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    return outputs


def publish(outputs, profile, region):
    identity = subprocess.run(['aws', 'sts', 'get-caller-identity', '--profile', profile,
                               '--region', region, '--output', 'json'],
                              check=True, capture_output=True, text=True)
    if json.loads(identity.stdout)['Account'] != outputs['AccountId'] or region != outputs['Region']:
        raise ValueError('AWS account/region does not match the deployment outputs')
    def aws(*args):
        subprocess.run(['aws', *args, '--profile', profile, '--region', region], check=True)
    bucket = outputs['SiteBucket']
    aws('s3', 'sync', str(STAGING / 'assets'), f's3://{bucket}/assets', '--no-progress',
        '--cache-control', 'public,max-age=31536000,immutable')
    # Publish the config last; never copy the bundled local-mode configuration.
    for filename, content_type in [('index.html', 'text/html'), ('config.json', 'application/json')]:
        aws('s3', 'cp', str(STAGING / filename), f's3://{bucket}/{filename}', '--no-progress',
            '--cache-control', 'no-store', '--content-type', content_type)
    aws('cloudfront', 'create-invalidation', '--distribution-id', outputs['DistributionId'],
        '--paths', '/', '/index.html', '/config.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outputs', type=Path, required=True)
    parser.add_argument('--stack', default='AutoSDELocal-dev')
    parser.add_argument('--profile', default=os.environ.get('AWS_PROFILE'),
                        required=not os.environ.get('AWS_PROFILE'))
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--publish', action='store_true', help='Upload and invalidate CloudFront (requires deployment approval)')
    args = parser.parse_args()
    outputs = stage_site(args.outputs, args.stack)
    if args.publish:
        publish(outputs, args.profile, args.region)
    else:
        print('Staged cloud frontend in .build/site. No upload performed.')
