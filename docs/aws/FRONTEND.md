# Frontend runtime configuration and login

The same built UI runs locally and in AWS. It fetches `/config.json` with `cache: no-store` before mounting the application or making API calls. The packaged file (`frontend/public/config.json`, copied to `autosde/static/config.json` by Vite) selects local mode:

```json
{"mode":"local","apiBaseUrl":""}
```

Local requests stay on the page's origin. The session endpoint supplies the existing per-start token, which is sent in `X-AutoSDE-Token` on writes. No Cognito UI or login redirect is involved.

## Cloud configuration

During deployment, replace the uploaded `config.json` with public deployment values; do not rebuild JavaScript or change the source default:

```json
{
  "mode": "cloud",
  "apiBaseUrl": "https://example.execute-api.us-east-1.amazonaws.com/dev",
  "uploadOrigin": "https://documents-bucket.s3.us-east-1.amazonaws.com",
  "cognito": {
    "domain": "https://example.auth.us-east-1.amazoncognito.com",
    "clientId": "public-app-client-id",
    "redirectUri": "https://app.example.com/",
    "logoutUri": "https://app.example.com/",
    "scopes": ["openid", "email", "profile"]
  }
}
```

`apiBaseUrl` is the origin plus optional API Gateway stage, **without** `/api`: the client appends `/api/...`. An empty value uses same-origin `/api` routing through CloudFront. Callback and logout URLs default to the current origin plus `/`; configured URLs must belong to the current origin and must not contain query strings or fragments. Configuration load failures stop startup instead of falling back to an unauthenticated local mode. Configuration contains no secrets.

## Hosting contract for the infrastructure step

- Use a Cognito **public app client without a client secret**, authorization-code grant, S256 PKCE, and the configured scopes. Register the callback and logout URLs exactly. Keep signup invite-only as planned. Register separate localhost callback URLs only if cloud-auth development is needed.
- API Gateway must validate the Cognito **access token**, its issuer and the public client ID as audience/client ID. If authorization scopes are required, add that resource-server scope to the app client and `cognito.scopes`. Access tokens may not contain email; the UI falls back to the session's user subject.
- For a cross-origin API, configure API Gateway CORS for the exact frontend origin, `GET`, `POST`, and `OPTIONS`, with `Authorization` and `Content-Type` request headers. Preflight must not require a JWT. Browser cookies are not sent to the cloud API.
- Serve `/config.json` and callback HTML without caching (CloudFront cache disabled and `Cache-Control: no-store`); never rewrite a missing configuration file to `index.html`. Hashed assets can use long-lived immutable caching. Keep configuration outside any long-lived asset cache.
- Set cloud CSP `connect-src` to the frontend, configured API origin, Cognito domain and exact document-bucket upload origin; keep `script-src 'self'`, existing font/style/image sources, and `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`. Set `Referrer-Policy: no-referrer` so callback query parameters are not disclosed to external font requests. Local CSP remains unchanged.
- Serve callback paths through the same app HTML. The existing hash routes require no server rewrite, and the saved hash restores the selected project, review, agent or knowledge base after login.
- Include `config.json` in the deployment artifact. It is included in the Python package too via `static/*.json`.

## Session behavior

Sign-in generates random state and a PKCE verifier using Web Crypto, stores a ten-minute transaction in tab-scoped `sessionStorage`, and opens Cognito's hosted authorization endpoint. The callback consumes the transaction, removes OAuth parameters from browser history, checks state and age, exchanges the code, and restores the saved hash route. Tokens are bound to the current API/client/domain/callback configuration.

Access and refresh tokens remain in tab-scoped `sessionStorage`, never local storage or URLs. Requests refresh an expiring access token once, sharing the refresh across concurrent requests. This uses Cognito's `refresh_token` grant; refresh-token rotation is supported when Cognito returns a replacement token. A rejected refresh or API 401 returns to the sign-in screen without replaying writes. A 403 remains a permission error. Closing the tab ends the locally stored session; do not rely on that as server-side revocation.

Sign-out clears local tokens and opens Cognito's `/logout` endpoint to clear its hosted-login session. It does not revoke already-issued JWTs; those expire normally. SARIF downloads use authenticated fetch and a temporary download URL rather than an unauthenticated API link.

## Validation

`npm test` in `frontend/` covers config validation, callback/state rejection, PKCE challenge matching, bookmark restoration, refresh concurrency, logout, cloud request headers, authenticated SARIF requests, and local CSRF-token behavior. `npm run build` regenerates the packaged UI. The live Cognito/API Gateway round trip needs deployed infrastructure and remains part of the deployment smoke test.

Protocol references: [Cognito authorization endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html), [token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html), and [logout endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/logout-endpoint.html).

The implemented same-origin CloudFront stack and publication commands are documented in [Deployment and review](DEPLOYMENT.md). Cloud document uploads go directly to the private S3 bucket using short-lived signed forms; both cloud and local uploads accept up to 20 MB per document. `uploadOrigin` must match the signed form’s HTTPS origin. S3 CORS permits POST only from the deployed frontend origin.
