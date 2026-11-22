"""Bot check (reCAPTCHA Enterprise) and Google sign-in verification.

Both are switched on by environment variables, so local runs and tests work
without them:
  RECAPTCHA_SITE_KEY   key created with `gcloud recaptcha keys create`
  GOOGLE_CLIENT_ID     OAuth web client ID for "Sign in with Google"
  GOOGLE_CLOUD_PROJECT project that owns the reCAPTCHA key
"""

import os

RECAPTCHA_SITE_KEY = os.environ.get("RECAPTCHA_SITE_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "assam-initiatives")
MIN_SCORE = 0.5


def recaptcha_enabled() -> bool:
    return bool(RECAPTCHA_SITE_KEY)


def human_ok(token: str, action: str = "register") -> bool:
    """True when reCAPTCHA thinks this is a person (or the check is switched off)."""
    if not recaptcha_enabled():
        return True
    if not token:
        return False
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    r = AuthorizedSession(creds).post(
        f"https://recaptchaenterprise.googleapis.com/v1/projects/{PROJECT}/assessments",
        json={"event": {"token": token, "siteKey": RECAPTCHA_SITE_KEY, "expectedAction": action}},
        timeout=10,
    )
    if r.status_code != 200:
        return False
    a = r.json()
    props = a.get("tokenProperties", {})
    score = a.get("riskAnalysis", {}).get("score", 0)
    return bool(props.get("valid")) and props.get("action") == action and score >= MIN_SCORE


def verify_google(credential: str) -> dict:
    """Checks a Google ID token; returns {email, name, sub} or raises ValueError."""
    if not GOOGLE_CLIENT_ID:
        raise ValueError("sign-in is not configured")
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token
    info = id_token.verify_oauth2_token(credential, greq.Request(), GOOGLE_CLIENT_ID)
    if not info.get("email_verified"):
        raise ValueError("email not verified")
    return {"email": info["email"].lower(), "name": info.get("name", ""), "sub": info["sub"]}
