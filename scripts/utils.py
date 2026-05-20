#!/usr/bin/env python3
# Adapted from fullstack-solution-template-for-agentcore
"""
Shared utilities for test scripts

Provides essential functions for SSM parameter discovery, AWS resource fetching, and authentication.
Uses /{project}/{env}/ SSM parameter path convention.
"""

import base64
import json
import os
import sys
import uuid
from typing import Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from colorama import Fore, Style, init

init(autoreset=True)

DEFAULT_PROJECT = "agentcore-workshop"
DEFAULT_ENV = "dev"


def get_ssm_prefix(project: Optional[str] = None, env: Optional[str] = None) -> str:
    """Return SSM prefix /{project}/{env}."""
    project = project or os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
    env = env or os.environ.get("ENVIRONMENT", DEFAULT_ENV)
    return f"/{project}/{env}"


def get_ssm_param(name: str, project: Optional[str] = None, env: Optional[str] = None) -> str:
    """Fetch a single SSM parameter under /{project}/{env}/{name}."""
    ssm = boto3.client("ssm")
    full_name = f"{get_ssm_prefix(project, env)}/{name}"
    try:
        return ssm.get_parameter(Name=full_name)["Parameter"]["Value"]
    except Exception as e:
        print_msg(f"Failed to fetch SSM parameter {full_name}: {e}", "error")
        sys.exit(1)


def get_ssm_params(
    *param_names: str, project: Optional[str] = None, env: Optional[str] = None
) -> Dict[str, str]:
    """Fetch multiple SSM parameters under /{project}/{env}/."""
    return {name: get_ssm_param(name, project, env) for name in param_names}


def get_workshop_config(project: Optional[str] = None, env: Optional[str] = None) -> Dict:
    """
    Get workshop configuration from SSM parameters and environment.

    Returns dict with project, env, region, and a helper to fetch SSM params.
    """
    project = project or os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
    env = env or os.environ.get("ENVIRONMENT", DEFAULT_ENV)
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    return {
        "project": project,
        "env": env,
        "region": region,
        "ssm_prefix": get_ssm_prefix(project, env),
    }


def _compute_secret_hash(username: str, client_id: str, client_secret: str) -> str:
    """Compute Cognito SECRET_HASH = Base64(HMAC_SHA256(client_secret, username + client_id))."""
    import hmac
    import hashlib
    message = username + client_id
    dig = hmac.new(client_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(dig).decode()


def authenticate_cognito(
    user_pool_id: str, client_id: str, username: str, password: str
) -> Tuple[str, str, str]:
    """
    Authenticate with Cognito and return (access_token, id_token, user_id).
    Handles app clients with or without a client secret.
    """
    print("\nAuthenticating...")
    cognito = boto3.client("cognito-idp")

    try:
        try:
            cognito.admin_get_user(UserPoolId=user_pool_id, Username=username)
        except cognito.exceptions.UserNotFoundException:
            print_msg(f"User '{username}' does not exist", "error")
            sys.exit(1)

        auth_params = {"USERNAME": username, "PASSWORD": password}

        # If the app client has a secret, compute SECRET_HASH
        try:
            client_desc = cognito.describe_user_pool_client(
                UserPoolId=user_pool_id, ClientId=client_id
            )["UserPoolClient"]
            client_secret = client_desc.get("ClientSecret")
            if client_secret:
                auth_params["SECRET_HASH"] = _compute_secret_hash(username, client_id, client_secret)
        except Exception:
            pass  # If we can't describe the client, try without SECRET_HASH

        response = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client_id,
            AuthParameters=auth_params,
        )

        access_token = response["AuthenticationResult"]["AccessToken"]
        id_token = response["AuthenticationResult"]["IdToken"]

        payload = id_token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        token_data = json.loads(base64.b64decode(payload))
        user_id = token_data.get("sub")

        print_msg("Authentication successful")
        print(f"  User ID: {user_id}")
        return access_token, id_token, user_id

    except Exception as e:
        print_msg(f"Authentication failed: {e}", "error")
        sys.exit(1)


def create_bedrock_client(region: str) -> boto3.client:
    """Create bedrock-agentcore client."""
    return boto3.client("bedrock-agentcore", region_name=region)


def generate_session_id() -> str:
    """Generate UUID4 session ID."""
    return str(uuid.uuid4())


def print_msg(message: str, level: str = "info") -> None:
    """Print formatted message."""
    prefixes = {
        "success": f"{Fore.GREEN}✓ ",
        "error": f"{Fore.RED}✗ ",
        "info": f"{Fore.YELLOW}ℹ ",
    }
    print(f"{prefixes.get(level, '')}{message}{Style.RESET_ALL}")


def print_section(title: str, width: int = 60) -> None:
    """Print section header."""
    print("\n" + "=" * width)
    print(title)
    print("=" * width + "\n")


def create_mock_jwt(user_id: str) -> str:
    """Create a mock unsigned JWT with user_id as 'sub' claim for local testing."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": user_id}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}."
