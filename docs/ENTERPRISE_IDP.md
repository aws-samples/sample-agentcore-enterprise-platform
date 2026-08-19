# Module 4 — Federating an Enterprise IdP

Module 4's headline is that your users keep their corporate logins. This is how
you actually wire that up, verified end to end against a real Microsoft Entra ID
tenant.

The stack supports **Entra ID**, **Okta**, and **Ping** as OIDC providers into
Cognito (`stacks/auth_stack.py`). Cognito stays in the path — it is the token
issuer every agent and the gateway validate against — and your IdP becomes the
place where humans actually authenticate.

```
Browser → Cognito Hosted UI → your IdP (login, MFA, conditional access)
                            ← id_token
        ← Cognito code → tokens minted by COGNITO (what agents verify)
```

Two consequences worth understanding before you start:

- **Agents never see your IdP's tokens.** They verify Cognito-issued JWTs
  (`docs/IDENTITY.md`), so nothing downstream changes when you federate.
- **The client secret lives in Secrets Manager**, never in CDK context or
  `platform.yaml`. Only the secret's *name* is configuration.

---

## Entra ID, end to end

Everything below was run against a live tenant. Substitute your own tenant id
and app name.

### 1. Get the redirect URI Cognito will use

It is derived from the project, environment, and account, so you can compute it
before the IdP exists:

```bash
DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name <prefix>-auth \
  --query "Stacks[0].Outputs[?OutputKey=='DomainUrl'].OutputValue" --output text)
echo "$DOMAIN/oauth2/idpresponse"
# https://<prefix>-<account>.auth.<region>.amazoncognito.com/oauth2/idpresponse
```

If the auth stack does not exist yet, deploy it once with `IDP_TYPE=cognito`
(module 3) and read the value, then come back.

### 2. Register the application in Entra ID

```bash
az ad app create \
  --display-name "AgentCore Accelerator" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "https://<prefix>-<account>.auth.<region>.amazoncognito.com/oauth2/idpresponse" \
  --enable-id-token-issuance true \
  --query "{appId:appId,id:id}"
```

`--enable-id-token-issuance` matters: Cognito's OIDC provider expects an
`id_token` from the implicit/hybrid response, and without it the login fails
after the user has already authenticated — the most confusing possible place.

Note the returned `appId`; that is your `IDP_CLIENT_ID`.

### 3. Put the client secret in Secrets Manager

```bash
az ad app credential reset --id <appId> \
  --display-name agentcore --years 1 \
  --query password -o tsv \
  | tr -d '\n\r' \
  | aws secretsmanager create-secret \
      --name agentcore/entra-client-secret \
      --secret-string file:///dev/stdin \
      --query Name --output text
```

**Do not omit the `tr -d '\n\r'`.** A trailing newline is stored as part of the
secret, Cognito passes it verbatim to Entra's token endpoint, and the exchange
fails with an invalid-client error that gives no hint about whitespace. Verify:

```bash
aws secretsmanager get-secret-value --secret-id agentcore/entra-client-secret \
  --output json | python3 -c "import json,sys; s=json.load(sys.stdin)['SecretString']; print(len(s), s==s.strip())"
# expect: 40 True     (length varies; `True` is the part that matters)
```

Do not use `--output text` for this check — the CLI appends its own newline and
you will chase a bug that is not there.

**Already have the secret in Secrets Manager?** Name it and the deploy will use
it as-is — no prompt, no copy:

```yaml
identity:
  client_secret_name: my-corp/entra-client-secret   # any name you already own
```

`./scripts/deploy.sh` checks that secret exists and passes the name through. If
you also supply `IDP_CLIENT_SECRET` (rotating the value), it writes into that
same secret rather than creating one of its own.

### 4. Deploy with federation on

```bash
IDP_TYPE=entra_id \
IDP_TENANT_ID=<tenant-id> \
IDP_CLIENT_ID=<appId> \
IDP_CLIENT_SECRET_NAME=agentcore/entra-client-secret \
./scripts/deploy.sh deploy --module 4
```

Or declare it in `platform.yaml` (see `presets/migration.yaml`):

```yaml
identity:
  idp: entra_id
  tenant_id: "<tenant-id>"
  client_id: "<appId>"
  client_secret_name: agentcore/entra-client-secret
```

The stack builds the issuer URL as
`https://login.microsoftonline.com/<tenant-id>/v2.0`, requests
`openid email profile`, and maps Entra's `email` and `name` claims onto the
Cognito user. The secret reaches CloudFormation as a
`{{resolve:secretsmanager:...}}` dynamic reference, so its value never appears
in the synthesized template or in process arguments.

### 5. Verify without a browser

Four checks, in the order that isolates failures:

```bash
# a) the provider exists and points at your tenant
aws cognito-idp describe-identity-provider \
  --user-pool-id <pool-id> --provider-name EntraID \
  --query 'IdentityProvider.{issuer:ProviderDetails.oidc_issuer,scopes:ProviderDetails.authorize_scopes,attrs:AttributeMapping}'

# b) the app clients actually offer it
aws cognito-idp describe-user-pool-client \
  --user-pool-id <pool-id> --client-id <app-client-id> \
  --query 'UserPoolClient.SupportedIdentityProviders'
# expect: ["COGNITO", "EntraID"]

# c) Entra accepts the client_id + redirect_uri (a sign-in page means both are registered;
#    it does NOT prove the app is fully sign-in ready — that only shows up in step 6)
curl -s -o /dev/null -D - \
  "$DOMAIN/oauth2/authorize?identity_provider=EntraID&client_id=<app-client-id>&response_type=code&scope=openid+email+profile&redirect_uri=<url-encoded-callback>" \
  | grep -i '^location:'
# then fetch that Microsoft URL: HTTP 200 with a sign-in page, no AADSTS code

# d) the secret Cognito holds is genuinely valid at Entra
curl -s -X POST "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" \
  -d grant_type=client_credentials -d client_id=<appId> \
  --data-urlencode "client_secret=<the secret>" \
  -d 'scope=https://graph.microsoft.com/.default'
# expect an access_token; "invalid_client" means the secret is wrong or has whitespace
```

Check (d) is the one worth knowing about: it proves the credential works
*without* completing a login, which separates "my secret is wrong" from "my
user cannot sign in".

### 6. Sign in for real

The token exchange and the attribute mapping only run when a human actually
signs in, so finish in a browser:

```bash
open "$DOMAIN/oauth2/authorize?identity_provider=EntraID&client_id=<app-client-id>&response_type=code&scope=openid+email+profile&redirect_uri=<url-encoded-callback>"
```

Sign in with a tenant user. The browser lands on your callback URL with
`?code=…` — a connection error there is expected if nothing is listening on
localhost, the code in the URL is the success signal.

Confirm Cognito created the federated user:

```bash
aws cognito-idp list-users --user-pool-id <pool-id> \
  --query 'Users[].{u:Username,status:UserStatus,email:Attributes[?Name==`email`].Value|[0]}'
# [{"u": "EntraID_<opaque-id>", "status": "EXTERNAL_PROVIDER", "email": "you@corp.example"}]
```

`EXTERNAL_PROVIDER` with a populated `email` is the proof: the exchange
succeeded and the attribute mapping ran. Note the username carries the provider
name exactly as Cognito registered it — `EntraID_…`, not lowercased.

To see what the agent will receive, exchange the code for tokens (within five
minutes, single use) and read the claims:

```bash
curl -s -X POST "$DOMAIN/oauth2/token" \
  -u "<app-client-id>:<app-client-secret>" \
  -d grant_type=authorization_code -d "code=<the code>" \
  --data-urlencode "redirect_uri=<the same callback>"
```

The `id_token` is issued by **Cognito**, not by Entra:

```json
{
  "iss": "https://cognito-idp.<region>.amazonaws.com/<pool-id>",
  "identities": [{ "providerName": "EntraID", "providerType": "OIDC", "primary": "true" }],
  "cognito:username": "EntraID_<opaque-id>",
  "email": "you@corp.example",
  "name": "Your Name"
}
```

That is the whole point of keeping Cognito in the path: the agent verifies a
token it already knows how to verify, and the federation is invisible to it.

### 7. Invoke an agent as that user

The runtime's JWT authorizer trusts the Cognito pool and a list of client ids
(`allowedClients`), which includes the app client — so the token you just
obtained works against the data plane with no further wiring:

```bash
curl -s -X POST \
  "https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded-runtime-arn>/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer <access_token from step 6>" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"who am I to you?","runtimeSessionId":"<33+ chars>"}'
```

Worth knowing what this does and does not check: the authorizer validates the
signature, the issuer, and the **client id** — not the scopes. A user token
carrying only `openid profile email` (no `agentcore/invoke`) is accepted. If you
need scope-level authorization, enforce it in the agent, not here.

### Turning it back off

Redeploying with `IDP_TYPE=cognito` removes the identity provider and drops it
from the app clients. It does **not** remove the users it created — they stay in
the pool as `EXTERNAL_PROVIDER` accounts with no provider behind them, and
nobody can sign in as them. Clean up if you care:

```bash
aws cognito-idp admin-delete-user --user-pool-id <pool-id> --username EntraID_<id>
```

---

## Okta and Ping

Same shape, one difference: they take a full `issuer_url` instead of a tenant
id, because the stack cannot derive it.

```bash
IDP_TYPE=okta \
IDP_ISSUER_URL=https://<your-org>.okta.com \
IDP_CLIENT_ID=<client-id> \
IDP_CLIENT_SECRET_NAME=agentcore/okta-client-secret \
./scripts/deploy.sh deploy --module 4
```

Register the same `…/oauth2/idpresponse` redirect URI on the Okta/Ping
application side, and grant the `openid email profile` scopes. The provider
appears in Cognito as `Okta` or `PingIdentity` respectively; use that name for
`identity_provider=` in the authorize URL.

---

## Verified

Against a live Entra ID tenant, 2026-08-19:

| Step | Result |
|---|---|
| App registration with the Cognito redirect URI | created, `AzureADMyOrg`, id-token issuance on |
| Secret stored in Secrets Manager and consumed via dynamic reference | value absent from the synthesized template |
| Cognito OIDC provider | created — issuer `…/<tenant>/v2.0`, scopes `openid email profile`, `email`/`name` mapped |
| App + web clients | list `EntraID` alongside `COGNITO` |
| Cognito → Entra handoff | redirects with our `client_id`; Entra serves the sign-in page, no AADSTS error |
| The secret Cognito holds | Entra issued a token for it (`client_credentials`) |
| Switching `IDP_TYPE` back and forth (`cognito` → `entra_id`) | provider removed and recreated cleanly |
| **Interactive sign-in** in a browser | completed; Cognito returned an authorization code |
| Federated user in Cognito | `EntraID_<id>`, `EXTERNAL_PROVIDER`, `email` and `name` mapped from Entra |
| Code → token exchange | Cognito minted `id_token` / `access_token` / `refresh_token`, `iss` = the Cognito pool |
| Agent invoke with that user's token | `200`, the orchestrator answered |
| Service principal | **not needed as a separate step** — Entra created it during the first sign-in (checked by deleting it and signing in again) |

---

## When it does not work

| Symptom | Cause |
|---|---|
| `redirect_mismatch` from Cognito | the `redirect_uri` you passed is not in the app client's callback URLs (default: `http://localhost:3000/api/auth/callback/cognito`) |
| `AADSTS50011` from Microsoft | the `…/oauth2/idpresponse` URI is not registered on the Entra app |
| `AADSTS700016` | wrong `IDP_CLIENT_ID`, or the app is in a different tenant |
| `invalid_client` at token exchange | the secret is wrong, expired, or has trailing whitespace (see step 3) |
| `invalid_client_secret` from **Cognito's** `/oauth2/token` | you sent another client's secret — the app client and the M2M client each have their own; read it with `describe-user-pool-client` for the client id you are authenticating as |
| Login succeeds, then fails returning to Cognito | `--enable-id-token-issuance` was not set on the Entra app |
| No `EntraID` button in the hosted UI | `IDP_TYPE` was not `entra_id` at deploy time — check `./scripts/deploy.sh config` |

The client secret is readable from Cognito by anyone with
`cognito-idp:DescribeIdentityProvider` (it is returned in `ProviderDetails`).
That is AWS behaviour, not something this stack can hide — treat that permission
as sensitive and prefer scoping it away from broad read roles.
