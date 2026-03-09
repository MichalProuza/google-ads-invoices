"""
Gmail API sender module.
Sends an email with PDF attachments using OAuth2 credentials.

Required environment variables:
  GMAIL_CREDENTIALS_JSON  – contents of the OAuth2 client_secret JSON file
  GMAIL_TOKEN_JSON        – contents of the previously-obtained token JSON
                            (or leave empty to trigger first-time auth flow)
  SENDER_EMAIL            – Gmail address to send from
"""

import base64
import json
import os
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _get_gmail_credentials() -> Credentials:
    """
    Loads Gmail OAuth2 credentials from environment variables.

    Expects:
      GMAIL_TOKEN_JSON       – JSON string of the stored token (access + refresh)
      GMAIL_CREDENTIALS_JSON – JSON string of the OAuth2 client secret

    On first run (no token), you must generate the token locally using
    generate_token.py and store the resulting JSON as a GitHub Secret.
    """
    token_json = os.environ.get("GMAIL_TOKEN_JSON", "")
    credentials_json = os.environ.get("GMAIL_CREDENTIALS_JSON", "")

    if not credentials_json:
        raise ValueError("GMAIL_CREDENTIALS_JSON environment variable is not set.")
    if not token_json:
        raise ValueError(
            "GMAIL_TOKEN_JSON environment variable is not set. "
            "Run generate_token.py locally first to obtain a token."
        )

    token_data = json.loads(token_json)
    creds_data = json.loads(credentials_json)

    client_config = creds_data.get("installed") or creds_data.get("web")
    if not client_config:
        raise ValueError("Invalid GMAIL_CREDENTIALS_JSON format.")

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        logger.info("Gmail token expired, refreshing ...")
        creds.refresh(Request())

    return creds


def _build_message(
    sender: str,
    to: str,
    subject: str,
    body: str,
    attachments: list[dict],
) -> str:
    """Builds a base64-encoded RFC 2822 MIME message."""
    message = MIMEMultipart()
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject

    message.attach(MIMEText(body, "plain", "utf-8"))

    for attachment in attachments:
        part = MIMEApplication(attachment["data"], _subtype="pdf")
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=attachment["filename"],
        )
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return raw


def send_invoices_email(
    to: str,
    subject: str,
    body: str,
    attachments: list[dict],
) -> None:
    """
    Sends an email with the given attachments via Gmail API.

    Args:
        to:          Recipient email address.
        subject:     Email subject.
        body:        Plain-text email body.
        attachments: List of dicts with keys: filename (str), data (bytes), mimetype (str).
    """
    sender = os.environ.get("SENDER_EMAIL", "")
    if not sender:
        raise ValueError("SENDER_EMAIL environment variable is not set.")

    creds = _get_gmail_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    raw_message = _build_message(sender, to, subject, body, attachments)

    # Calculate raw message size for logging
    raw_size_mb = len(raw_message) / (1024 * 1024)
    logger.info("Email message size: %.2f MB", raw_size_mb)

    try:
        result = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )
        logger.info("Email sent successfully. Message ID: %s", result.get("id"))
    except HttpError as exc:
        logger.error("Failed to send email via Gmail API: %s", exc)
        if exc.resp.status == 400:
            logger.error(
                "Hint: 'Precondition check failed' usually means the Gmail OAuth token "
                "has expired (tokens for apps in 'testing' mode expire after 7 days). "
                "Either regenerate GMAIL_TOKEN_JSON or publish the OAuth app in Google Cloud Console."
            )
        raise
