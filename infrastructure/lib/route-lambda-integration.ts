import {
  Stack,
  aws_apigatewayv2 as apigw,
  aws_iam as iam,
  aws_lambda as lambda,
} from 'aws-cdk-lib';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';

// CDK 2.274's default permission inserts a literal {proxy+} into SourceArn.
// The route ARN converts path parameters to wildcards and retains the HTTP method.
export class RouteLambdaIntegration extends HttpLambdaIntegration {
  constructor(
    id: string,
    private readonly target: lambda.IFunction
  ) {
    super(id, target);
  }

  protected override completeBind(options: apigw.HttpRouteIntegrationBindOptions): void {
    this.target.addPermission('InvokeFromRoute', {
      scope: options.scope,
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: options.route.routeArn,
      sourceAccount: Stack.of(options.route).account,
    });
  }
}
