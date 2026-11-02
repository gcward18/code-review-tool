import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const KEYS = new Set(['AWS_ACCOUNT_ID', 'AWS_PROFILE', 'AWS_REGION', 'AUTOSDE_STAGE']);

export interface DeploymentConfig {
  account: string;
  region: string;
  profile: string;
  stage: string;
}

// Only read public deployment coordinates; never import the sibling project's credentials/settings.
export function loadConfig(
  env = process.env,
  root = resolve(__dirname, '../..'),
  requireAccount = true
): DeploymentConfig {
  const envFile = env.AUTOSDE_DEPLOY_ENV || resolve(root, '../Code Review Service/.env');
  const values: Record<string, string> = {};
  if (existsSync(envFile)) {
    for (const line of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
      const match = line.match(/^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$/);
      if (match && KEYS.has(match[1])) values[match[1]] = match[2].replace(/^(['"])(.*)\1$/, '$2');
    }
  } else if (env.AUTOSDE_DEPLOY_ENV) {
    throw new Error('AUTOSDE_DEPLOY_ENV does not exist.');
  }
  const get = (key: string) => env[key] || values[key];
  const account = get('AWS_ACCOUNT_ID') || env.CDK_DEFAULT_ACCOUNT || '';
  const region = get('AWS_REGION') || env.CDK_DEFAULT_REGION || '';
  const profile = get('AWS_PROFILE') || '';
  const stage = get('AUTOSDE_STAGE') || '';
  if (requireAccount && !/^\d{12}$/.test(account))
    throw new Error('Set AWS_ACCOUNT_ID or use CDK with the configured AWS profile.');
  if (!/^[a-z]{2}-[a-z]+-\d$/.test(region)) throw new Error('Set AWS_REGION.');
  if (!/^[a-z][a-z0-9-]{0,19}$/.test(stage))
    throw new Error('Set AUTOSDE_STAGE (lowercase, max 20 characters).');
  if (!profile) throw new Error('Set AWS_PROFILE.');
  return { account, region, profile, stage };
}
