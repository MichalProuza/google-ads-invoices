"""
One-time script to generate Gmail OAuth2 token.

Run this LOCALLY before setting up GitHub Actions:
  python generate_token.py

It will open a browser for Google OAuth consent, then print the token JSON.
Copy the output and store it as the GMAIL_TOKEN_JSON GitHub Secret.

Prerequisites:
  1. Go to Google Cloud Console → APIs & Services → Credentials
  2. Create an OAuth 2.0 Client ID (Desktop app)
  3. Download the JSON and set GMAIL_CREDENTIALS_JSON env var (or pass the path below)
"""

import json
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "client_secret.json")

flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
creds = flow.run_local_server(port=0)

token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes),
}

print("\n✅ Token generated successfully!\n")
print("Copy the following JSON and store it as the GMAIL_TOKEN_JSON GitHub Secret:\n")
print(json.dumps(token_data, indent=2))
