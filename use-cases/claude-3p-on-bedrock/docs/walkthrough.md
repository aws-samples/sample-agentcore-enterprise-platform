# Set up the network, Entra ID and devices

The tasks your teams do around the deployment of
[Claude Code and Cowork on Amazon Bedrock](../README.md). Each task names
who does it and how to check it's done.

| Task | Who | When |
|---|---|---|
| [Connect the corporate network](#connect-the-corporate-network) | Network team | Before deploying |
| [Choose the hostname](#choose-the-hostname) | DNS team | Before deploying |
| [Get a certificate](#get-a-certificate) | PKI or cloud team | Before deploying |
| [Register the Entra application](#register-the-entra-application) | Entra administrator | Before deploying |
| [Prepare the AWS account](#prepare-the-aws-account) | Cloud team | Before deploying |
| [Add the DNS record](#add-the-dns-record) | DNS team | After deploying |
| [Connect laptops](#connect-laptops) | Endpoint management (MDM) | After deploying |
| [Test with pilot users](#test-with-pilot-users) | One user per team | Before go-live |

The setup wizard asks for the values these tasks produce:

| Value | Example | From |
|---|---|---|
| Corporate ranges developers connect from | `172.16.0.0/16` | Network team |
| Gateway hostname | `claude.corp.example.com` | DNS team |
| Certificate in ACM | (picked from a list) | PKI or cloud team |
| Entra tenant ID and application ID | two GUIDs | Entra administrator |
| Entra client secret | (typed hidden) | Entra administrator |
| Allowed email domains | `corp.example.com` | Entra administrator |
| One App Role per team | `Claude.Engineering` | Entra administrator |

## Connect the corporate network

**Who: network team.** Laptops reach the gateway only over your private
network, never over the internet.

1. **Check the address plan.** The platform network is `10.0.0.0/16`
   (private subnets `10.0.2.0/24` and `10.0.3.0/24`), and this can't be
   changed in `platform.yaml` today. If your corporate network already uses
   `10.0.0.0/16`, stop and talk to the platform owners before the first deploy.
2. **Connect the networks** with AWS Site-to-Site VPN, a Transit Gateway or
   Direct Connect, attached to the platform VPC. Its ID is in SSM at
   `/<project>/<env>/networking/vpc-id` once the network is deployed.
3. **Route both ways and open the port:**

| Where | What |
|---|---|
| Corporate routers or VPN appliance | `10.0.0.0/16` → the tunnel |
| Platform VPC private route tables | your corporate ranges → the VPN gateway or Transit Gateway attachment |
| Corporate firewalls | TCP **443** from your corporate ranges to `10.0.0.0/16` |

The platform doesn't create the VPN or these routes for you.

**Done when** a laptop on the corporate network reaches a test machine in a
platform private subnet on port 443.

## Choose the hostname

**Who: DNS team.** Pick a name in a domain your corporate DNS serves, for
example `claude.corp.example.com`. It must only ever resolve to private
addresses: Claude Code refuses to sign in to a gateway on a public address.

Choose how the name resolves:

| Option | When | What to do |
|---|---|---|
| **Corporate DNS** (recommended) | Your DNS can resolve public AWS names (most can) | After deploying, add one CNAME record ([Add the DNS record](#add-the-dns-record)) |
| **Route 53** | Your DNS can't resolve AWS names | The stack creates the record in a Route 53 private zone. You also need a [Route 53 Resolver inbound endpoint](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-forwarding-inbound-queries.html) and a conditional forward of the zone from your DNS. |

To check the first option, look up any existing internal AWS load balancer
name from a corporate laptop. It should return private addresses:

```bash
nslookup internal-example-123456789.eu-west-1.elb.amazonaws.com
```

## Get a certificate

**Who: PKI or cloud team.** The certificate must be in AWS Certificate
Manager (ACM), **in the Region you deploy to**, and cover the hostname.

| Your hostname | Use |
|---|---|
| In a public domain you own (e.g. `corp.example.com`), even if only your internal DNS serves the name | **A public ACM certificate.** Free, renews itself, trusted by every laptop. The wizard can request it. ACM checks you own the domain with one CNAME in the domain's **public** DNS; the load balancer still stays internal. |
| In an internal-only domain (e.g. `.internal`, `.corp`) | **A certificate from your company PKI**, imported into ACM. Laptops must already trust your company CA. Re-import it before it expires. |

**Done when** the certificate shows **Issued** in ACM.

## Register the Entra application

**Who: Entra administrator.** One application for the gateway. Claude
Desktop/Cowork signs in through the gateway, so it needs nothing extra.

1. **Register it.** Entra admin center → **App registrations → New registration**:
   - Supported account types: **Accounts in this organizational directory only**
   - Redirect URI: platform **Web**, `https://<hostname>/oauth/callback`

   Note the **Directory (tenant) ID** and the **Application (client) ID** from the Overview page.
2. **Create a client secret.** **Certificates & secrets → New client secret.**
   Copy the **Value** right away (it's shown only once) and pass it to the
   person running the wizard over a secure channel, never by email or chat.
   Put the expiry date in your calendar ([Rotate the client secret](changes.md#rotate-the-entra-client-secret)).
3. **Grant permissions.** **API permissions → Add → Microsoft Graph → Delegated:**
   `openid`, `profile`, `email`, `offline_access`, then **Grant admin consent**.
4. **Include the email address.** **Token configuration → Add optional claim → ID → `email`.**
   The gateway only admits allowed email domains. For users without a mailbox,
   the wizard can fall back to their sign-in name.
5. **Create one App Role per team.** **App roles → Create app role**, for
   example display name `Claude Engineering`, member types **Users/Groups**,
   value **`Claude.Engineering`**. The value is what the wizard asks for:
   copy it exactly, it's case-sensitive.
6. **Assign people.** **Enterprise applications** → the app:
   - **Properties → Assignment required → Yes**, so nobody else can sign in.
   - **Users and groups → Add user/group** → pick people and their App Role.
     Assigning *groups* needs Entra ID P1 or P2; on the free tier, assign users.

A person assigned without a team role gets the default policy. A person not
assigned at all can't sign in.

## Prepare the AWS account

**Who: cloud team.**

1. **Store the client secret.** The wizard does this: it asks for the value
   hidden and stores it in Secrets Manager. To do it by hand instead:
   ```bash
   umask 077; read -rs S; printf '%s' "$S" > /tmp/s.txt; unset S
   aws secretsmanager create-secret --name claude-3p/entra-oidc --secret-string file:///tmp/s.txt; rm -f /tmp/s.txt
   ```
   Don't give the secret a name ending in a hyphen and six characters (like
   `-secret`): AWS reads that as part of its ID and the gateway can't read it.
   The wizard checks this.
2. **Enable Claude on Bedrock.** In the Bedrock console, open an Anthropic
   model and complete Anthropic's one-time use-case form, if nobody in the
   account has.
3. **Check your Region restrictions.** `eu.`, `us.` and `global.` models send
   each request to whichever Region in that area has capacity. If a service
   control policy (SCP) blocks some Regions, Bedrock must be allowed in **all**
   of them, or requests fail at random. To list a model's Regions:
   ```bash
   aws bedrock get-inference-profile --inference-profile-identifier eu.anthropic.claude-haiku-4-5-20251001-v1:0 --query 'models[].modelArn' --output text | tr '\t' '\n'
   ```

## Add the DNS record

**Who: DNS team, after deploying.** Only with the corporate DNS option; with
Route 53 the stack has already created it.

Add a **CNAME** (not an A record: the load balancer's addresses change) from
the hostname to the stack output `AlbDnsName`:

```
claude.corp.example.com.   300   IN   CNAME   internal-<name>.<region>.elb.amazonaws.com.
```

**Done when**, from a corporate laptop:

```bash
nslookup claude.corp.example.com                  # private addresses only
curl -s -o /dev/null -w '%{http_code}\n' https://claude.corp.example.com/healthz   # 200
```

## Connect laptops

**Who: endpoint management.** Laptops need Claude Code 2.1.285 or later (`claude update`).

**Claude Code** (terminal and IDEs): deploy this `managed-settings.json`
with your MDM, or as the same keys in a configuration profile (macOS domain
`com.anthropic.claudecode`):

```json
{
  "forceLoginMethod": "gateway",
  "forceLoginGatewayUrl": "https://claude.corp.example.com",
  "parentSettingsBehavior": "merge",
  "allowedProviders": ["gateway"]
}
```

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

**Claude Desktop and Cowork:** set `bootstrapUrl` to the stack output
`DesktopBootstrapUrl` (`https://claude.corp.example.com/user/bootstrap`). The
easiest way is to build the profile in Claude Desktop and export it for your
MDM ([Deploy with MDM](https://claude.com/docs/third-party/claude-desktop/mdm)).

**Certificate fingerprint:** on first sign-in, Claude Code asks developers to
trust the gateway's certificate. Publish the expected fingerprint on your
intranet so they can compare. Developers see its first 16 characters:

```bash
openssl s_client -connect claude.corp.example.com:443 -servername claude.corp.example.com </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f'
```

## Test with pilot users

One pilot user per team, plus one with no team role:

| Test | Expected |
|---|---|
| Claude Code: `claude`, then `/login` | Browser sign-in with Entra, then signed in |
| Claude Desktop: launch | Gateway sign-in, then Cowork works |
| The model picker | Exactly the team's models |
| A prompt | An answer |
| A user not assigned to the app | Entra refuses the sign-in |
| A laptop off the VPN | Gateway unreachable |

Some models appear twice in Claude Desktop (standard and 1M context). That's
expected; see [Show each model once](changes.md#show-each-model-only-once-in-claude-desktop).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/login` says the gateway is on a public address | Serve the hostname only from corporate DNS, pointing at the load balancer |
| `nslookup` finds nothing | Add the CNAME, or use the Route 53 option |
| Connection times out | Check routes both ways and the firewall, and that the laptop's range is in the corporate ranges you gave the wizard |
| Entra `AADSTS50011` | The redirect URI must be exactly `https://<hostname>/oauth/callback`, platform **Web** |
| Entra `AADSTS50105` | Assign the person to the app |
| Signed in, but the team's models are missing | The App Role value doesn't match exactly (it's case-sensitive) |
| "The provider rejected a test request" / `403 AccessDenied` | Complete the Bedrock use-case form, or allow Bedrock in every Region your SCP blocks |
| Sign-in suddenly fails for everyone | The Entra client secret expired: [rotate it](changes.md#rotate-the-entra-client-secret) |
