"""One-time setup: register the Asana OAuth app with AgentCore Identity and give consent.

Run by an operator with AWS credentials for the factory's account, after the SoftwareFactory
stack is deployed in Asana mode (it creates the workload identity the sync runs as).

  1. The client secret is read from the terminal and sent straight to AgentCore Identity,
     which stores it. It is never printed, logged or written to disk.
  2. The script prints the callback URL to add to the Asana app's redirect URLs.
  3. It prints a one-time authorization URL. Open it, sign in to Asana, approve.
  4. It completes the session and checks the token by reading the project's sections.

After this, AgentCore Identity refreshes the Asana token by itself; the sync Lambda needs no
login and holds no secret. Re-run with --reconsent to approve again (e.g. new scopes).

    python factory/sync/asana_identity_setup.py --client-id <id> --project <gid>
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asana_sync as s

ASANA = {
    "issuer": "https://app.asana.com",
    "authorizationEndpoint": "https://app.asana.com/-/oauth_authorize",
    "tokenEndpoint": "https://app.asana.com/-/oauth_token",
    "responseTypes": ["code"],
}
WORKLOAD = "software-factory-asana-sync"
RETURN_URL = "http://localhost:8765/asana-consent-done"  # must match the stack's workload identity


def provider(ctl, client_id: str, secret: str) -> dict:
    cfg = {
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"authorizationServerMetadata": ASANA},
            "clientId": client_id,
            "clientSecret": secret,
            "clientAuthenticationMethod": "CLIENT_SECRET_POST",
        }
    }
    try:
        ctl.get_oauth2_credential_provider(name=s.PROVIDER)
    except ctl.exceptions.ResourceNotFoundException:
        return ctl.create_oauth2_credential_provider(
            name=s.PROVIDER,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput=cfg,
        )
    return ctl.update_oauth2_credential_provider(
        name=s.PROVIDER,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput=cfg,
    )


def main() -> int:
    import boto3

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--client-id", required=True)
    p.add_argument(
        "--project",
        default=os.environ.get("FACTORY_ASANA_PROJECT"),
        required=not os.environ.get("FACTORY_ASANA_PROJECT"),
    )
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument(
        "--keep-secret",
        action="store_true",
        help="the provider exists; only (re)do the consent",
    )
    p.add_argument(
        "--scopes",
        default=" ".join(s.SCOPES),
        help="space-separated; 'default' if the Asana app has full permissions. "
        "The stack's -c asana_scopes must match.",
    )
    p.add_argument(
        "--reconsent",
        action="store_true",
        help="ask Asana for consent even if a token exists",
    )
    args = p.parse_args()
    s.SCOPES[:] = args.scopes.split()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ctl, dp = (
        session.client("bedrock-agentcore-control"),
        session.client("bedrock-agentcore"),
    )

    if not args.keep_secret:
        secret = getpass.getpass("Asana app client secret (input hidden): ").strip()
        if not secret:
            print("no secret entered", file=sys.stderr)
            return 1
        out = provider(ctl, args.client_id, secret)
        del secret
        print(
            "\nAdd this redirect URL to the Asana app (developer console > your app > OAuth):"
        )
        print(f"  {out['callbackUrl']}\n")
        input("Press Enter once it is saved... ")

    resp = s.asana_token(dp, WORKLOAD, RETURN_URL, force=args.reconsent)
    if not resp.get("accessToken"):
        print("\nOpen this URL, sign in to Asana and approve the factory's access:")
        print(f"  {resp['authorizationUrl']}\n")
        print(
            "The browser then lands on a localhost page that does not load. That is expected."
        )
        input("Press Enter after approving... ")
        dp.complete_resource_token_auth(
            sessionUri=resp["sessionUri"], userIdentifier={"userId": s.TOKEN_USER}
        )
        resp = s.asana_token(dp, WORKLOAD, RETURN_URL)
    if not resp.get("accessToken"):
        print(
            f"no token yet (session {resp.get('sessionStatus')}); re-run with --keep-secret",
            file=sys.stderr,
        )
        return 2
    board = s.RestBoard(resp["accessToken"], args.project)
    print(f"OK: the token reads the project ({len(board.sections())} sections).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
