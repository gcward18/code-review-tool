import { App } from 'aws-cdk-lib';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { AutoSdeStack } from '../lib/autosde-stack';
import { loadConfig } from '../lib/config';

const config = loadConfig();
execFileSync('python3', [resolve(__dirname, '../../scripts/build_lambda.py'), '--verify-only'], {
  stdio: 'inherit',
});
const app = new App({ analyticsReporting: false });
new AutoSdeStack(app, `AutoSDELocal-${config.stage}`, {
  env: { account: config.account, region: config.region },
  stage: config.stage,
  runtimeAsset: resolve(__dirname, '../../.build/lambda'),
  frontendAsset: resolve(__dirname, '../../autosde/static'),
});
app.synth();
