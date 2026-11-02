import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { afterAll, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AutoSdeStack } from '../lib/autosde-stack';

const fixture = mkdtempSync(join(tmpdir(), 'autosde-cdk-'));
for (const path of ['runtime/autosde', 'runtime/bin', 'site/assets'])
  mkdirSync(join(fixture, path), { recursive: true });
for (const path of [
  'runtime/autosde/aws.py',
  'runtime/bin/gitleaks',
  'site/index.html',
  'site/config.json',
])
  writeFileSync(join(fixture, path), 'fixture');
const stack = new AutoSdeStack(new App({ analyticsReporting: false }), 'Test', {
  env: { account: '123456789012', region: 'us-east-1' },
  stage: 'test',
  runtimeAsset: join(fixture, 'runtime'),
  frontendAsset: join(fixture, 'site'),
});
const template = Template.fromStack(stack);
afterAll(() => rmSync(fixture, { recursive: true, force: true }));

describe('retained private storage matching the shared data schema', () => {
  it('uses on-demand DynamoDB with both query indexes and recovery', () => {
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'autosde-test',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
      BillingMode: 'PAY_PER_REQUEST',
      DeletionProtectionEnabled: true,
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
      SSESpecification: { SSEEnabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      GlobalSecondaryIndexes: Match.arrayWith(
        ['GSI1', 'GSI2'].map((name) =>
          Match.objectLike({
            IndexName: name,
            Projection: { ProjectionType: 'ALL' },
            KeySchema: [
              { AttributeName: `${name}PK`, KeyType: 'HASH' },
              { AttributeName: `${name}SK`, KeyType: 'RANGE' },
            ],
          })
        )
      ),
    });
    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });
  it('keeps both encrypted buckets private and retains them, with temporary-upload expiry', () => {
    template.resourceCountIs('AWS::S3::Bucket', 2);
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'autosde-local-123456789012-us-east-1-test-documents',
      CorsConfiguration: {
        CorsRules: [
          {
            AllowedMethods: ['POST'],
            AllowedHeaders: ['content-type'],
            AllowedOrigins: [Match.anyValue()],
            MaxAge: 600,
          },
        ],
      },
    });
    for (const resource of Object.values(template.findResources('AWS::S3::Bucket'))) {
      expect(resource.DeletionPolicy).toBe('Retain');
      expect(resource.Properties.VersioningConfiguration.Status).toBe('Enabled');
      expect(resource.Properties.PublicAccessBlockConfiguration).toEqual({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      });
      expect(
        resource.Properties.BucketEncryption.ServerSideEncryptionConfiguration[0]
          .ServerSideEncryptionByDefault.SSEAlgorithm
      ).toBe('AES256');
    }
    template.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Prefix: 'uploads/',
            ExpirationInDays: 1,
            NoncurrentVersionExpiration: { NoncurrentDays: 1 },
          }),
        ]),
      },
    });
    const policies = Object.values(template.findResources('AWS::S3::BucketPolicy'));
    expect(policies).toHaveLength(2);
    for (const policy of policies)
      expect(policy.Properties.PolicyDocument.Statement).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            Effect: 'Deny',
            Condition: { Bool: { 'aws:SecureTransport': 'false' } },
          }),
        ])
      );
  });
});

describe('runtime and least-privilege permissions', () => {
  it('packages two versioned Python runtimes with fixed limits and no concurrency reservation', () => {
    template.resourceCountIs('AWS::Lambda::Function', 2);
    template.resourceCountIs('AWS::Lambda::Alias', 2);
    template.resourceCountIs('AWS::Lambda::Version', 2);
    const functions = Object.values(template.findResources('AWS::Lambda::Function'));
    for (const fn of functions) {
      expect(fn.Properties.Runtime).toBe('python3.12');
      expect(fn.Properties.Architectures).toEqual(['x86_64']);
      expect(fn.Properties.ReservedConcurrentExecutions).toBeUndefined();
      expect(fn.Properties.Environment.Variables).toMatchObject({
        GITLEAKS_PATH: '/var/task/bin/gitleaks',
        SSM_PREFIX: '/autosde/test',
        WORKER_FUNCTION: 'autosde-local-test-worker:live',
      });
      expect(JSON.stringify(fn.Properties.Environment)).not.toMatch(
        /OPENAI_API_KEY|LANGSMITH_API_KEY|GITHUB_TOKEN/
      );
    }
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'autosde.aws.api',
      Timeout: 28,
      MemorySize: 1024,
      RecursiveLoop: 'Terminate',
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'autosde.aws.worker',
      Timeout: 840,
      MemorySize: 2048,
      RecursiveLoop: 'Allow',
    });
    for (const version of Object.values(template.findResources('AWS::Lambda::Version')))
      expect(version.DeletionPolicy).toBe('Retain');
    template.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
      MaximumRetryAttempts: 0,
      MaximumEventAgeInSeconds: 3600,
      Qualifier: Match.anyValue(),
    });
  });
  it('bounds log retention and does not enable API body logging', () => {
    template.resourceCountIs('AWS::Logs::LogGroup', 2);
    for (const group of Object.values(template.findResources('AWS::Logs::LogGroup')))
      expect(group.Properties.RetentionInDays).toBe(7);
    for (const stage of Object.values(template.findResources('AWS::ApiGatewayV2::Stage'))) {
      expect(stage.Properties.AccessLogSettings).toBeUndefined();
      expect(stage.Properties.DefaultRouteSettings).toMatchObject({
        ThrottlingBurstLimit: 10,
        ThrottlingRateLimit: 5,
      });
    }
  });
  it('grants only explicit application actions and scoped resources, with no managed admin policies', () => {
    const expectedActions = new Set([
      'logs:CreateLogStream',
      'logs:PutLogEvents',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:Query',
      's3:GetObject',
      's3:PutObject',
      's3:DeleteObject',
      's3:GetObjectVersion',
      's3:DeleteObjectVersion',
      'ssm:GetParametersByPath',
      'secretsmanager:GetSecretValue',
      'lambda:InvokeFunction',
    ]);
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    expect(policies).toHaveLength(2);
    for (const policy of policies) {
      const seen = new Set<string>();
      for (const statement of policy.Properties.PolicyDocument.Statement) {
        const actions = [statement.Action].flat();
        for (const action of actions) {
          expect(expectedActions.has(action)).toBe(true);
          seen.add(action);
        }
        for (const resource of [statement.Resource].flat()) expect(resource).not.toBe('*');
        if (actions.includes('secretsmanager:GetSecretValue'))
          expect(statement.Resource).toEqual([
            { Ref: 'OpenAISecretArn' },
            { Ref: 'GitHubSecretArn' },
            { Ref: 'LangSmithSecretArn' },
          ]);
        if (actions.includes('lambda:InvokeFunction'))
          expect(JSON.stringify(statement.Resource)).toContain('autosde-local-test-worker:live');
        if (actions.includes('s3:GetObject')) {
          const resources = JSON.stringify(statement.Resource);
          expect(resources).toContain('documents/*');
          expect(resources).toContain('uploads/*');
          expect(resources).not.toContain('Site');
        }
      }
      const allowed = new Set(expectedActions);
      if (!JSON.stringify(policy.Properties.Roles).includes('WorkerRole')) {
        allowed.delete('s3:GetObjectVersion');
        allowed.delete('s3:DeleteObjectVersion');
      }
      expect(seen).toEqual(allowed);
    }
    for (const role of Object.values(template.findResources('AWS::IAM::Role')))
      expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  });
  it('requires explicit model, prices and existing secret ARNs; stores no secret values in SSM', () => {
    const params = template.toJSON().Parameters;
    for (const name of [
      'ModelId',
      'InputPrice',
      'OutputPrice',
      'OpenAISecretArn',
      'GitHubSecretArn',
      'LangSmithSecretArn',
    ]) {
      expect(params[name]).toBeDefined();
      expect(params[name].Default).toBeUndefined();
    }
    template.resourceCountIs('AWS::SecretsManager::Secret', 0);
    template.resourceCountIs('AWS::SSM::Parameter', 7);
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/autosde/test/model',
      Value: { Ref: 'ModelId' },
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/autosde/test/openai_secret_id',
      Value: { Ref: 'OpenAISecretArn' },
    });
  });
});

describe('authenticated same-origin frontend', () => {
  it('uses invite-only Cognito, TOTP, PKCE-compatible public code client and exact callback URLs', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
      DeletionProtection: 'ACTIVE',
      EnabledMfas: ['SOFTWARE_TOKEN_MFA'],
      MfaConfiguration: 'OPTIONAL',
      Policies: { PasswordPolicy: Match.objectLike({ MinimumLength: 14 }) },
    });
    template.hasResource('AWS::Cognito::UserPool', { DeletionPolicy: 'Retain' });
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      GenerateSecret: false,
      AllowedOAuthFlows: ['code'],
      EnableTokenRevocation: true,
      AllowedOAuthScopes: Match.arrayWith(['openid', 'email', 'profile']),
      CallbackURLs: Match.anyValue(),
      LogoutURLs: Match.anyValue(),
    });
    const client = Object.values(template.findResources('AWS::Cognito::UserPoolClient'))[0]
      .Properties;
    expect(client.CallbackURLs).toEqual(client.LogoutURLs);
    expect(JSON.stringify(client.AllowedOAuthScopes)).toContain('/access');
    template.hasResourceProperties('AWS::Cognito::UserPoolResourceServer', {
      Identifier: 'autosde',
      Scopes: [{ ScopeName: 'access', ScopeDescription: 'Access the AutoSDE API' }],
    });
    expect(JSON.stringify(client.CallbackURLs)).toContain('DomainName');
  });
  it('protects every API route with JWT and an access-token scope', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'JWT',
      IdentitySource: ['$request.header.Authorization'],
    });
    const permissions = Object.values(template.findResources('AWS::Lambda::Permission'));
    expect(permissions).toHaveLength(2);
    for (const permission of permissions) {
      const arn = JSON.stringify(permission.Properties.SourceArn);
      expect(arn).not.toContain('{proxy+}');
      expect(arn).toMatch(/\/\*\/(GET|POST)\/api\/\*/);
      expect(permission.Properties.SourceAccount).toBe('123456789012');
    }
    const routes = Object.values(template.findResources('AWS::ApiGatewayV2::Route'));
    expect(routes).toHaveLength(2);
    expect(routes.map((r) => r.Properties.RouteKey).sort()).toEqual([
      'GET /api/{proxy+}',
      'POST /api/{proxy+}',
    ]);
    for (const route of routes)
      expect(route.Properties).toMatchObject({
        AuthorizationType: 'JWT',
        AuthorizationScopes: ['autosde/access'],
      });
  });
  it('uses OAC and disables config/API caching without rewriting errors to HTML', () => {
    template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
    template.resourceCountIs('AWS::CloudFront::Distribution', 1);
    const distribution = Object.values(template.findResources('AWS::CloudFront::Distribution'))[0]
      .Properties.DistributionConfig;
    expect(distribution.DefaultRootObject).toBe('index.html');
    expect(distribution.DefaultCacheBehavior.CachePolicyId).toBe(
      '4135ea2d-6df8-44a3-9df3-4b5a84be39ad'
    );
    expect(distribution.DefaultCacheBehavior.ViewerProtocolPolicy).toBe('redirect-to-https');
    const api = distribution.CacheBehaviors.find(
      (b: { PathPattern: string }) => b.PathPattern === 'api/*'
    );
    expect(api.CachePolicyId).toBe(distribution.DefaultCacheBehavior.CachePolicyId);
    expect(api.OriginRequestPolicyId).toBe('b689b0a8-53d0-40ab-baf2-68738e2966ac');
    expect(api.AllowedMethods).toContain('POST');
    for (const error of distribution.CustomErrorResponses) {
      expect(error.ResponsePagePath).toBeUndefined();
      expect(error.ErrorCachingMinTTL).toBe(0);
    }
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        CustomHeadersConfig: {
          Items: [{ Header: 'Cache-Control', Value: 'no-store', Override: true }],
        },
        SecurityHeadersConfig: Match.objectLike({
          ReferrerPolicy: { ReferrerPolicy: 'no-referrer', Override: true },
        }),
      }),
    });
    const config = template.toJSON().Outputs.RuntimeConfig.Value;
    expect(JSON.stringify(config)).toContain('cloud');
    expect(JSON.stringify(config)).toContain('uploadOrigin');
    expect(JSON.stringify(config)).toContain(
      'autosde-local-123456789012-us-east-1-test-documents.s3'
    );
    expect(JSON.stringify(config)).toContain('autosde/access');
  });
  it('puts security headers in the CloudFront security section so deployment is accepted', () => {
    for (const policy of Object.values(
      template.findResources('AWS::CloudFront::ResponseHeadersPolicy')
    )) {
      const config = policy.Properties.ResponseHeadersPolicyConfig;
      expect(config.SecurityHeadersConfig.ContentTypeOptions).toEqual({ Override: true });
      const custom = config.CustomHeadersConfig?.Items || [];
      expect(custom.map((header: { Header: string }) => header.Header.toLowerCase())).not.toContain(
        'x-content-type-options'
      );
    }
  });
  it('rejects missing deployment assets before producing a broken deployment', () => {
    expect(
      () =>
        new AutoSdeStack(new App(), 'Missing', {
          stage: 'dev',
          runtimeAsset: '/missing',
          frontendAsset: '/missing',
        })
    ).toThrow(/Build the runtime/);
  });
});
