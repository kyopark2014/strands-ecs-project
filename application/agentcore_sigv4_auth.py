import httpx
import boto3
import json
import logging
import os
import urllib.request
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

logger = logging.getLogger("agentcore-sigv4-auth")


def _fetch_container_credentials() -> Credentials | None:
    """Resolve ECS/Fargate task role credentials from the container metadata endpoint."""
    uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if not uri:
        relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        if relative:
            uri = f"http://169.254.170.2{relative}"

    if not uri:
        return None

    request = urllib.request.Request(uri)
    token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
    if token:
        request.add_header("Authorization", token)

    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))

    access_key = (payload.get("AccessKeyId") or "").strip()
    secret_key = (payload.get("SecretAccessKey") or "").strip()
    session_token = (payload.get("Token") or "").strip()
    if not access_key or not secret_key:
        return None

    return Credentials(access_key, secret_key, session_token or None)


def resolve_frozen_credentials():
    """Return frozen AWS credentials for AgentCore Gateway SigV4 signing."""
    container_creds = _fetch_container_credentials()
    if container_creds is not None:
        return container_creds.get_frozen_credentials()

    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials available for AgentCore Gateway SigV4 auth"
        )

    frozen = credentials.get_frozen_credentials()
    if not frozen.access_key:
        raise RuntimeError(
            "Empty AWS access key for AgentCore Gateway SigV4 auth"
        )
    return frozen


class AgentCoreSigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore"):
        self.region = region
        self.service = service

    def auth_flow(self, request: httpx.Request):
        credentials = resolve_frozen_credentials()
        headers = dict(request.headers)
        body = request.content

        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=body,
            headers=headers,
        )
        SigV4Auth(credentials, self.service, self.region).add_auth(aws_request)
        prepared = aws_request.prepare()

        for key, value in prepared.headers.items():
            request.headers[key] = value

        yield request
