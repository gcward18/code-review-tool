import {
  ArnFormat,
  CfnOutput,
  CfnParameter,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_apigatewayv2 as apigw,
  aws_apigatewayv2_authorizers as authorizers,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
  aws_cognito as cognito,
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_ssm as ssm,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { RouteLambdaIntegration } from './route-lambda-integration';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

export interface AutoSdeStackProps extends StackProps {
  stage: string;
  runtimeAsset: string;
  frontendAsset: string;
}

export class AutoSdeStack extends Stack {
  constructor(scope: Construct, id: string, props: AutoSdeStackProps) {
    super(scope, id, props);
    const { stage } = props;
    if (!/^[a-z][a-z0-9-]{0,19}$/.test(stage)) throw new Error('Invalid deployment stage.');
    for (const path of [
      join(props.runtimeAsset, 'autosde/aws.py'),
      join(props.runtimeAsset, 'bin/gitleaks'),
      join(props.frontendAsset, 'index.html'),
      join(props.frontendAsset, 'config.json'),
    ]) {
      if (!existsSync(path))
        throw new Error('Build the runtime and frontend assets before synthesis.');
    }
    const prefix = `/autosde/${stage}`;
    const name = `autosde-local-${stage}`;
    const parameter = (id: string, description: string, allowedPattern: string) =>
      new CfnParameter(this, id, {
        type: 'String',
        description,
        allowedPattern,
        noEcho: id.endsWith('SecretArn'),
      });
    const model = parameter('ModelId', 'Operator-selected OpenAI model ID.', '[a-zA-Z0-9._:/-]+');
    const inputPrice = parameter(
      'InputPrice',
      'USD per million input tokens for this model.',
      '[0-9]+(\\.[0-9]+)?'
    );
    const outputPrice = parameter(
      'OutputPrice',
      'USD per million output tokens for this model.',
      '[0-9]+(\\.[0-9]+)?'
    );
    const secretPattern = `arn:aws:secretsmanager:${this.region}:${this.account}:secret:[a-zA-Z0-9/_+=.@-]+-[a-zA-Z0-9]{6}`;
    const secretArns = ['OpenAI', 'GitHub', 'LangSmith'].map((label) =>
      parameter(
        `${label}SecretArn`,
        `Complete ARN of the existing ${label} secret (not its value).`,
        secretPattern
      )
    );

    const table = new dynamodb.Table(this, 'Data', {
      tableName: `autosde-${stage}`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    for (const index of ['GSI1', 'GSI2'])
      table.addGlobalSecondaryIndex({
        indexName: index,
        partitionKey: { name: `${index}PK`, type: dynamodb.AttributeType.STRING },
        sortKey: { name: `${index}SK`, type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.ALL,
      });
    const bucketProps: s3.BucketProps = {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
    };
    const documentBucketName = `autosde-local-${this.account}-${this.region}-${stage}-documents`;
    const uploadOrigin = `https://${documentBucketName}.s3.${this.region}.${this.urlSuffix}`;
    const documents = new s3.Bucket(this, 'Documents', {
      bucketName: documentBucketName,
      ...bucketProps,
      lifecycleRules: [
        {
          id: 'ExpireTemporaryUploads',
          prefix: 'uploads/',
          expiration: Duration.days(1),
          noncurrentVersionExpiration: Duration.days(1),
          abortIncompleteMultipartUploadAfter: Duration.days(1),
        },
      ],
    });
    const site = new s3.Bucket(this, 'Site', bucketProps);
    const settings: Record<string, string> = {
      model: model.valueAsString,
      input_price: inputPrice.valueAsString,
      output_price: outputPrice.valueAsString,
      reasoning_effort: 'low',
      openai_secret_id: secretArns[0].valueAsString,
      github_secret_id: secretArns[1].valueAsString,
      langsmith_secret_id: secretArns[2].valueAsString,
    };
    const parameters = Object.entries(settings).map(
      ([key, value]) =>
        new ssm.StringParameter(this, `Setting-${key}`, {
          parameterName: `${prefix}/${key}`,
          stringValue: value,
          description: 'AutoSDE setting; contains no secret value.',
        })
    );

    const workerName = `${name}-worker`;
    const workerTarget = `${workerName}:live`;
    const workerArn = this.formatArn({
      service: 'lambda',
      resource: 'function',
      resourceName: workerTarget,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });
    const code = lambda.Code.fromAsset(props.runtimeAsset);
    const environment = {
      TABLE_NAME: table.tableName,
      BUCKET_NAME: documents.bucketName,
      SSM_PREFIX: prefix,
      WORKER_FUNCTION: workerTarget,
      GITLEAKS_PATH: '/var/task/bin/gitleaks',
    };
    const makeFunction = (
      label: string,
      handler: string,
      timeout: Duration,
      memorySize: number
    ) => {
      const functionName = label === 'Worker' ? workerName : `${name}-api`;
      const logGroup = new logs.LogGroup(this, `${label}Logs`, {
        logGroupName: `/aws/lambda/${functionName}`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.RETAIN,
      });
      const role = new iam.Role(this, `${label}Role`, {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      });
      const grant = (actions: string[], resources: string[]) =>
        role.addToPolicy(new iam.PolicyStatement({ actions, resources }));
      grant(
        ['logs:CreateLogStream', 'logs:PutLogEvents'],
        [
          this.formatArn({
            service: 'logs',
            resource: 'log-group',
            resourceName: `/aws/lambda/${functionName}:log-stream:*`,
            arnFormat: ArnFormat.COLON_RESOURCE_NAME,
          }),
        ]
      );
      grant(
        [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:Query',
        ],
        [table.tableArn]
      );
      grant(['dynamodb:Query'], [`${table.tableArn}/index/GSI1`, `${table.tableArn}/index/GSI2`]);
      grant(
        ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
        [documents.arnForObjects('documents/*'), documents.arnForObjects('uploads/*')]
      );
      if (label === 'Worker')
        grant(
          ['s3:GetObjectVersion', 's3:DeleteObjectVersion'],
          [documents.arnForObjects('uploads/*')]
        );
      grant(
        ['ssm:GetParametersByPath'],
        [
          this.formatArn({
            service: 'ssm',
            resource: 'parameter',
            resourceName: `autosde/${stage}/*`,
          }),
        ]
      );
      grant(
        ['secretsmanager:GetSecretValue'],
        secretArns.map((secret) => secret.valueAsString)
      );
      grant(['lambda:InvokeFunction'], [workerArn]);
      const fn = new lambda.Function(this, label, {
        functionName,
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.X86_64,
        code,
        handler,
        environment,
        timeout,
        memorySize,
        role,
        logGroup,
        tracing: lambda.Tracing.DISABLED,
        // Only the worker intentionally chains bounded batch steps to itself.
        recursiveLoop:
          label === 'Worker' ? lambda.RecursiveLoop.ALLOW : lambda.RecursiveLoop.TERMINATE,
      });
      for (const setting of parameters) fn.node.addDependency(setting);
      const version = fn.currentVersion;
      version.applyRemovalPolicy(RemovalPolicy.RETAIN);
      return { fn, alias: new lambda.Alias(this, `${label}Live`, { aliasName: 'live', version }) };
    };
    const worker = makeFunction('Worker', 'autosde.aws.worker', Duration.minutes(14), 2048);
    worker.alias.configureAsyncInvoke({ retryAttempts: 0, maxEventAge: Duration.hours(1) });
    const api = makeFunction('Api', 'autosde.aws.api', Duration.seconds(28), 1024);
    const http = new apigw.HttpApi(this, 'HttpApi', { apiName: name, createDefaultStage: true });
    if (http.defaultStage?.node.defaultChild instanceof apigw.CfnStage) {
      http.defaultStage.node.defaultChild.defaultRouteSettings = {
        throttlingBurstLimit: 10,
        throttlingRateLimit: 5,
        detailedMetricsEnabled: false,
      };
    }

    const pool = new cognito.UserPool(this, 'Users', {
      userPoolName: name,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      passwordPolicy: {
        minLength: 14,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: Duration.days(1),
      },
      removalPolicy: RemovalPolicy.RETAIN,
      deletionProtection: true,
    });
    const domainPrefix = `autosde-local-${stage}-${this.account}`;
    pool.addDomain('HostedLogin', { cognitoDomain: { domainPrefix } });
    const loginDomain = `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`;
    const headers = new cloudfront.ResponseHeadersPolicy(this, 'SiteHeaders', {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            'font-src https://fonts.gstatic.com',
            "img-src 'self' data:",
            `connect-src 'self' ${loginDomain} ${uploadOrigin}`,
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
          ].join('; '),
        },
        contentTypeOptions: { override: true },
        frameOptions: { override: true, frameOption: cloudfront.HeadersFrameOption.DENY },
        referrerPolicy: {
          override: true,
          referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER,
        },
        strictTransportSecurity: {
          override: true,
          accessControlMaxAge: Duration.days(365),
          includeSubdomains: true,
        },
      },
      customHeadersBehavior: {
        customHeaders: [{ header: 'Cache-Control', value: 'no-store', override: true }],
      },
    });
    const siteOrigin = origins.S3BucketOrigin.withOriginAccessControl(site);
    const distribution = new cloudfront.Distribution(this, 'Website', {
      comment: name,
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: siteOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        responseHeadersPolicy: headers,
      },
      additionalBehaviors: {
        'assets/*': {
          origin: siteOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          responseHeadersPolicy: new cloudfront.ResponseHeadersPolicy(this, 'AssetHeaders', {
            securityHeadersBehavior: { contentTypeOptions: { override: true } },
          }),
        },
        'api/*': {
          origin: new origins.HttpOrigin(
            `${http.httpApiId}.execute-api.${this.region}.${this.urlSuffix}`
          ),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      },
      // Do not rewrite errors: a missing config.json must fail startup, and API errors stay API errors.
      errorResponses: [403, 404].map((status) => ({
        httpStatus: status,
        ttl: Duration.seconds(0),
      })),
    });
    const siteUrl = `https://${distribution.distributionDomainName}/`;
    documents.addCorsRule({
      allowedOrigins: [`https://${distribution.distributionDomainName}`],
      allowedMethods: [s3.HttpMethods.POST],
      allowedHeaders: ['content-type'],
      maxAge: 600,
    });
    const resourceServer = pool.addResourceServer('ApiScopes', {
      identifier: 'autosde',
      scopes: [{ scopeName: 'access', scopeDescription: 'Access the AutoSDE API' }],
    });
    const client = pool.addClient('Browser', {
      userPoolClientName: name,
      generateSecret: false,
      preventUserExistenceErrors: true,
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(1),
      enableTokenRevocation: true,
      oAuth: {
        flows: { authorizationCodeGrant: true, implicitCodeGrant: false },
        callbackUrls: [siteUrl],
        logoutUrls: [siteUrl],
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
          cognito.OAuthScope.resourceServer(resourceServer, {
            scopeName: 'access',
            scopeDescription: 'Access the AutoSDE API',
          }),
        ],
      },
    });
    const authorizer = new authorizers.HttpUserPoolAuthorizer('Cognito', pool, {
      userPoolClients: [client],
    });
    http.addRoutes({
      path: '/api/{proxy+}',
      methods: [apigw.HttpMethod.GET, apigw.HttpMethod.POST],
      integration: new RouteLambdaIntegration('ApiIntegration', api.alias),
      authorizer,
      authorizationScopes: ['autosde/access'],
    });
    const runtimeConfig = {
      mode: 'cloud',
      apiBaseUrl: '',
      uploadOrigin,
      cognito: {
        domain: loginDomain,
        clientId: client.userPoolClientId,
        redirectUri: siteUrl,
        logoutUri: siteUrl,
        scopes: ['openid', 'email', 'profile', 'autosde/access'],
      },
    };
    for (const [key, value] of Object.entries({
      AccountId: this.account,
      Region: this.region,
      Stage: stage,
      SiteUrl: siteUrl,
      ApiUrl: http.apiEndpoint,
      SiteBucket: site.bucketName,
      DocumentBucket: documents.bucketName,
      TableName: table.tableName,
      DistributionId: distribution.distributionId,
      UserPoolId: pool.userPoolId,
      UserPoolClientId: client.userPoolClientId,
      WorkerFunction: workerTarget,
      RuntimeConfig: JSON.stringify(runtimeConfig),
    }))
      new CfnOutput(this, key, { value });
  }
}
