import { expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../lib/config';

it('reads only public deployment settings and lets explicit environment values override the file', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autosde-env-'));
  const path = join(dir, '.env');
  writeFileSync(
    path,
    'AWS_ACCOUNT_ID=123456789012\nAWS_PROFILE="dev-profile"\nAWS_REGION=us-east-1\nAUTOSDE_STAGE=dev\nOPENAI_API_KEY=private-sentinel\n'
  );
  try {
    expect(loadConfig({ AUTOSDE_DEPLOY_ENV: path, AUTOSDE_STAGE: 'test' })).toEqual({
      account: '123456789012',
      profile: 'dev-profile',
      region: 'us-east-1',
      stage: 'test',
    });
  } finally {
    rmSync(dir, { recursive: true });
  }
});
it('refuses incomplete or malformed deployment coordinates without looking up AWS', () => {
  const valid = {
    AWS_ACCOUNT_ID: '123456789012',
    AWS_PROFILE: 'test',
    AWS_REGION: 'us-east-1',
    AUTOSDE_STAGE: 'dev',
  };
  for (const change of [
    { AWS_ACCOUNT_ID: 'invalid' },
    { AWS_REGION: 'invalid' },
    { AUTOSDE_STAGE: '../escape' },
  ])
    expect(() => loadConfig({ ...valid, ...change }, '/missing')).toThrow();
  expect(() => loadConfig({ ...valid, AUTOSDE_DEPLOY_ENV: '/missing/env' })).toThrow(
    /does not exist/
  );
});
