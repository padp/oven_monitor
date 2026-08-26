"""Typed Plex API calls for enriching oven data with real production context.

Mirrors the sibling "Fetch Log Data" project's collector/plex.py: a persistent
requests.Session carrying the live ASID/AUTH_PROD session, with one retry on
401/403/419 that re-authenticates via plex_login.renew_credentials() and picks
the request back up - so a expired session self-heals instead of needing a
human to re-capture cookies from a browser (which is how this integration
started - see reference/plex_curl.txt and reference/plex_curl_2.txt, both
one-off manual captures used only to discover these two endpoints' shape).

Two calls, matching the two-step lookup described in
reference/plex_curl_2.txt: get_furnace_loads() returns recent load cycles for
a workcenter (temperature, actual start/end time, and the container serial
numbers loaded into each cycle); get_container() takes one of those serial
numbers and returns its part/quantity.

Uses the SAME shared login/session files "Fetch Log Data" already logs in with
and keeps refreshed - Extrusion DB/secret/login_infos.txt and .../infos.txt,
one level up from every sibling project's own folder (see
plex_login.LOGIN_SECRETS_PATH / SESSION_SECRETS_PATH) - rather than a separate
copy under this project's own secret/. One Plex session shared across every
script that needs it, so nothing here needs its own login setup.
"""

import requests

from . import plex_login

session = requests.Session()

_login_secrets = None  # lazy: only load_credentials() once, on first use
_secrets = None        # the live ASID/AUTH_PROD pair


def _ensure_logged_in():
    """Load the current session on first use; log in fresh if none exists yet."""
    global _login_secrets, _secrets
    if _secrets is not None:
        return
    _login_secrets = plex_login.load_credentials(plex_login.LOGIN_SECRETS_PATH)
    try:
        _secrets = plex_login.load_credentials(plex_login.SESSION_SECRETS_PATH)
        if not _secrets.get("ASID") or not _secrets.get("AUTH_PROD"):
            raise ValueError("incomplete session file")
    except (FileNotFoundError, ValueError):
        _reauth()
        return
    _apply_cookies(_secrets)


def _apply_cookies(creds):
    session.cookies.update({
        "plex-customercode": "Whitehall-KY",
        "plex-languageculturecode": "en-US",
        "plex-auth-prod": creds["AUTH_PROD"],
    })


def _reauth():
    """Log in fresh, overwrite the shared infos.txt, and pick up the new ASID/AUTH_PROD."""
    global _secrets
    _secrets = plex_login.renew_credentials(
        secrets_path=plex_login.SESSION_SECRETS_PATH,
        username=_login_secrets["username"],
        password=_login_secrets["password"],
        company_code=_login_secrets["company_code"],
    )
    _apply_cookies(_secrets)


def _post(url, params, json, timeout=15):
    """POST with a single retry: re-login once if the session has expired (401/403/419)."""
    _ensure_logged_in()
    resp = session.post(url, params={**params, "__asid": _secrets["ASID"]}, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        _reauth()
        resp = session.post(url, params={**params, "__asid": _secrets["ASID"]}, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        raise PermissionError("Session expired and re-login failed")

    resp.raise_for_status()
    return resp


def get_furnace_loads(workcenter_key, begin_date, end_date, active_only=True):
    """Recent furnace load cycles for one workcenter.

    workcenter_key: Plex's WorkcenterKey, e.g. "58085" for PAD-Small Aging Oven
      (confirmed 2026-08-26 via a captured search - see reference/plex_curl.txt).
    begin_date, end_date: ISO 8601 strings, e.g. "2026-08-25T05:00:00.000Z".
      Per the user's own note captured alongside the original request: the
      begin date should be one day before "today" to reliably catch the
      currently-running cycle, which may have started the previous day.

    Returns the raw list of load records (each with CyclesData - temperature,
    ActualStartTime/ActualEndTime - and ContainersData - SerialNo, JobNo,
    Quantity per container in that load). sourceActionKey=19727 is Plex's own
    internal identifier for this search screen, carried over from the capture
    as-is rather than guessed at.
    """
    resp = _post(
        "https://cloud.plex.com/ProductionTracking/FurnaceLoad/Search",
        params={"limit": "true", "sourceActionKey": "19727"},
        json={
            "ActiveOnly": active_only,
            "BeginDate": begin_date,
            "EndDate": end_date,
            "WorkcenterKey": str(workcenter_key),
        },
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("Data", {}).get("Rows", [])


def get_container(serial_no, start_date, end_date, pcn=270494):
    """Part/quantity lookup for one container, by serial number.

    The second step of the two-step flow: call get_furnace_loads() first, take
    a SerialNo from one of its ContainersData entries, then call this.

    pcn: the PCN value seen in the original capture (270494) - NOT confirmed
    as a fixed tenant-wide constant vs. something that varies per part/search.
    Left as a parameter with that observed value as the default rather than
    hardcoded, since it hasn't been verified either way yet.

    Search/FromPartMenu/Active/GroupBy are fixed UI-state flags for this
    specific search screen in the original capture, not real parameters, and
    are hardcoded here to match it. sourceActionKey=9580 is likewise carried
    over from the capture as-is.
    """
    resp = _post(
        "https://cloud.plex.com/Inventory/Container/Search",
        params={"limit": "true", "sourceActionKey": "9580"},
        json={
            "Search": False,
            "FromPartMenu": False,
            "GroupBy": "PartNo",
            "Active": True,
            "StartDate": start_date,
            "EndDate": end_date,
            "PCN": pcn,
            "SerialNo": serial_no,
        },
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("Data", {}).get("Rows", [])
