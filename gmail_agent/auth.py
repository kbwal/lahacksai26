"""Desktop OAuth credentials stay in macOS Keychain, never in the mail database."""

import json
import logging
import sys

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_agent.client import GmailClient

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
SERVICE = "lahacks-mail.gmail"


def keychain():
    if sys.platform != "darwin":
        raise RuntimeError("This app stores OAuth tokens in macOS Keychain and requires macOS")
    from keyring.backends.macOS import Keyring

    return Keyring()


class _KeychainCredentials(Credentials):
    def refresh(self, request):
        super().refresh(request)
        keychain().set_password(SERVICE, self.credential_ref, self.to_json())


def connect(client_secrets):
    config = json.loads(client_secrets.read_text())
    if "installed" not in config:
        raise ValueError("Use a Google Desktop OAuth client JSON, not a web client")
    # The library logs the callback URL, including its authorization code, at INFO.
    logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.WARNING)
    flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        timeout_seconds=180,
        authorization_prompt_message="Opening Google sign-in in your browser…",
        success_message="Gmail is connected. You can close this tab.",
        access_type="offline",
        prompt="consent",
    )
    granted = credentials.granted_scopes or credentials.scopes or []
    if not credentials.refresh_token or not set(SCOPES).issubset(granted):
        raise RuntimeError(
            "Google did not grant offline access and both required Gmail permissions"
        )
    session = AuthorizedSession(credentials)
    try:
        profile = GmailClient(session).profile()
    finally:
        session.close()
    email = profile["emailAddress"].lower()
    keychain().set_password(SERVICE, email, credentials.to_json())
    return email


def client_for(credential_ref):
    vault = keychain()
    saved = vault.get_password(SERVICE, credential_ref)
    if not saved:
        raise RuntimeError("Gmail credentials were not found in Keychain; reconnect this account")
    credentials = _KeychainCredentials.from_authorized_user_info(json.loads(saved), scopes=SCOPES)
    credentials.credential_ref = credential_ref
    # AuthorizedSession refreshes on demand inside the worker's retry boundary.
    return GmailClient(AuthorizedSession(credentials))


def disconnect(credential_ref):
    vault = keychain()
    saved = vault.get_password(SERVICE, credential_ref)
    if saved:
        token = json.loads(saved).get("refresh_token")
        response = requests.post(
            "https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=30
        )
        if response.status_code not in {200, 400}:
            raise RuntimeError("Google token revocation failed; credentials were kept for retry")
        vault.delete_password(SERVICE, credential_ref)
