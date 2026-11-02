import { spawnSync } from 'node:child_process';
import { loadConfig } from '../lib/config';

// The CLI resolves its account through the selected profile; the app validates the resulting account.
const config = loadConfig(process.env, undefined, false);
const args = process.argv.slice(2);
if (!args.length) throw new Error('Pass a CDK command, e.g. synth or diff.');
const result = spawnSync(
  process.execPath,
  [require.resolve('aws-cdk/bin/cdk'), ...args, '--profile', config.profile, '--no-notices'],
  {
    stdio: 'inherit',
    env: {
      ...process.env,
      AWS_PROFILE: config.profile,
      AWS_REGION: config.region,
      AUTOSDE_STAGE: config.stage,
    },
  }
);
process.exit(result.status ?? 1);
