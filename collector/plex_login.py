"""Plex Cloud (Plex ERP) programmatic login via requests.Session().

Ported from the sibling "Fetch Log Data" project's collector/login.py, which
replicates the same flow a browser goes through logging into cloud.plex.com by
hand: Login -> authorize -> interaction -> plexidp/login -> callback -> sso ->
asid. Kept close to identical rather than reworked - this is the proven,
working implementation, not something to redesign from scratch.

Install: pip install requests beautifulsoup4

Named plex_login.py (not login.py, matching the reference exactly) to avoid
shadowing anything named login.py elsewhere in this project.
"""

import os
import re
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

# From the reference implementation's own authorize URL. This is Plex Cloud's
# client id for its own standard web login flow (Whitehall-KY tenant), not
# something registered per-project - carried over unchanged.
CLIENT_ID = "B1371A12-25E2-4422-94ED-8E2983F81C66"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Absolute, not the reference's "../secret/..." - resolved from __file__ so it
# works regardless of the caller's working directory, matching this project's
# own collector/config.py and publisher/config.py convention (a relative path
# tied to CWD is exactly the class of bug the mapped-drive-letter Task
# Scheduler issue earlier in this project turned out to be).
LOGIN_SECRETS_PATH = os.path.join(_PROJECT_ROOT, "secret", "plex_login_infos.txt")
SESSION_SECRETS_PATH = os.path.join(_PROJECT_ROOT, "secret", "plex_infos.txt")


def extract_asid(resp: requests.Response) -> str | None:
    """
    ASID is not a cookie -- it's minted once per login and carried in the
    URL query string (?__asid=...) on subsequent navigations. Check the
    final URL first, then walk the redirect history, since it may appear
    partway through the chain rather than at the very end.
    """
    urls_to_check = [resp.url] + [r.url for r in resp.history]
    for url in urls_to_check:
        qs = parse_qs(urlparse(url).query)
        if "__asid" in qs:
            return qs["__asid"][0]
    return None


def login_and_get_credentials(username: str, password: str, company_code: str) -> dict:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    # Step 1: kick off login -> redirects through to accounts.plex.com/interaction
    interaction_resp = session.get(
        "https://cloud.plex.com/login",
        params={"ContinueUrl": "/", "handler": "navigateToIam"},
        allow_redirects=True,
    )
    interaction_resp.raise_for_status()

    # The final URL after redirects should be the /interaction page.
    # Pull authzId + clientId out of it (they're in the query string).
    final_url = interaction_resp.url
    authz_match = re.search(r"authzId%3D([^&%]+)|authzId=([^&]+)", final_url)
    client_match = re.search(r"clientId=([^&]+)", final_url)
    if not authz_match:
        raise RuntimeError(f"Could not find authzId in redirect chain. Final URL: {final_url}")
    authz_id = authz_match.group(1) or authz_match.group(2)
    client_id = client_match.group(1) if client_match else CLIENT_ID

    return_url = f"/connect/authorize/callback?authzId={authz_id}"

    # Step 2: POST credentials to plexidp/login
    login_post = session.post(
        "https://accounts.plex.com/plexidp/login",
        data={
            "username": username,
            "companyCode": company_code,
            "password": password,
            "redirect": "true",
            "returnUrl": return_url,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"},
        allow_redirects=True,
    )
    login_post.raise_for_status()

    # Step 3: If not auto-followed to callback, hit it explicitly
    if "connect/authorize/callback" not in login_post.url:
        callback_resp = session.get(
            "https://accounts.plex.com/connect/authorize/callback",
            params={"authzId": authz_id},
            allow_redirects=True,
        )
    else:
        callback_resp = login_post
    callback_resp.raise_for_status()

    # Step 4: parse the auto-submit form_post page for id_token + session_state
    soup = BeautifulSoup(callback_resp.text, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError(
            "Expected an auto-submit <form> with id_token/session_state but found none. "
            "Dumping response for inspection:\n" + callback_resp.text[:2000]
        )

    action_url = form.get("action")
    hidden_inputs = {
        inp.get("name"): inp.get("value")
        for inp in form.find_all("input", {"type": "hidden"})
    }
    id_token = hidden_inputs.get("id_token")
    if not id_token:
        raise RuntimeError(f"No id_token found in form. Hidden inputs were: {list(hidden_inputs.keys())}")

    # Step 5: POST to /sso to establish the cloud.plex.com session.
    # Pass through ALL hidden fields from the form, not just id_token --
    # OIDC form_post responses often include a 'state' field (needed to
    # correlate against .AspNetCore.Correlation.* cookie) and possibly
    # others depending on tenant/version. Omitting any of them can cause
    # the middleware to silently decline the request (falls through to
    # plain IIS routing, surfaces as a 405 with Allow: GET).
    sso_resp = session.post(
        action_url or "https://cloud.plex.com/sso",
        data=hidden_inputs,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"},
        allow_redirects=True,
    )
    sso_resp.raise_for_status()

    # Step 6: ASID is minted once per login and lives in the URL, not a
    # cookie. It should show up in the redirect chain triggered by /sso.
    asid = extract_asid(sso_resp)

    # If it's not in the history/final URL, some tenants require an
    # explicit hit to DefaultHome/launchpage before ASID gets assigned.
    if not asid:
        home_resp = session.get("https://cloud.plex.com/DefaultHome", allow_redirects=True)
        asid = extract_asid(home_resp)

    if not asid:
        # Last resort: dump what we have so this is debuggable rather
        # than failing silently. ASID may be embedded in the response
        # body (e.g. a JS redirect or link) rather than a real 3xx hop.
        raise RuntimeError(
            "Could not find __asid in redirect chain or DefaultHome. "
            f"sso_resp.url={sso_resp.url}, history={[r.url for r in sso_resp.history]}"
        )

    cookies = session.cookies.get_dict()
    creds = {
        "ASID": asid,
        "AUTH_PROD": cookies.get("plex-auth-prod"),
    }

    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(
            f"Login flow completed but missing credentials: {missing}. "
            f"All cookies captured: {list(cookies.keys())}"
        )

    return creds


def load_credentials(secrets_path: str) -> dict:
    """Read KEY=VALUE (one per line) from the secrets file."""
    creds = {}
    with open(secrets_path) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip()
    return creds


def save_credentials(secrets_path: str, creds: dict) -> None:
    """
    Overwrite the secrets file with fresh credentials. Writes to a sibling
    temp file and atomically replaces the target, so a crash mid-write can
    never leave secrets_path partially written.
    """
    tmp_path = f"{secrets_path}.tmp"

    with open(tmp_path, "w") as f:
        for key, value in creds.items():
            f.write(f"{key}={value}\n")

    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, secrets_path)


def renew_credentials(secrets_path: str, username: str, password: str, company_code: str) -> dict:
    """Log in fresh and overwrite the secrets file. Call this on a 419."""
    creds = login_and_get_credentials(username, password, company_code)
    save_credentials(secrets_path, creds)
    return creds


if __name__ == "__main__":
    # One-time manual bootstrap: python -m collector.plex_login
    # Logs in once and writes secret/plex_infos.txt, so the first call from
    # collector/plex.py has a session to load rather than needing to log in
    # itself on the very first use. Requires secret/plex_login_infos.txt
    # (username/password/company_code, KEY=value lines) to already exist.
    login_secrets = load_credentials(LOGIN_SECRETS_PATH)
    creds = login_and_get_credentials(
        username=login_secrets["username"],
        password=login_secrets["password"],
        company_code=login_secrets["company_code"],
    )
    save_credentials(SESSION_SECRETS_PATH, creds)
    print(f"Logged in - wrote {SESSION_SECRETS_PATH}")
