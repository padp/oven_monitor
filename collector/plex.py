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


def _post(url, params, json, timeout=45):
    """POST with a single retry: re-login once if the session has expired (401/403/419).

    45s, not the reference's 15s: a real live call against FurnaceLoad/Search
    (142 rows, workcenter 58085) measured at 17.8s on 2026-08-26 - Plex Cloud
    itself appears to just be this slow for a search over a multi-day window,
    not a sign of anything broken. 15s clipped that call every time.
    """
    _ensure_logged_in()
    resp = session.post(url, params={**params, "__asid": _secrets["ASID"]}, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        _reauth()
        resp = session.post(url, params={**params, "__asid": _secrets["ASID"]}, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        raise PermissionError("Session expired and re-login failed")

    resp.raise_for_status()
    return resp


# WorkcenterKey per oven, confirmed live 2026-08-26/27 (each search's rows came
# back labeled with the expected WorkcenterCode). WorkcenterCode itself is not
# a search input - Plex only takes the key - so it's recorded here purely for
# reference/logging, not passed to any request.
WORKCENTER_SMALL_OVEN = "58085"   # PAD-Small Aging Oven
WORKCENTER_LARGE_OVEN = "58084"   # PAD-Large Aging Oven

# FurnaceLoadStatusKey values, discovered live 2026-08-27 by inspecting the
# status breakdown of ~140-220 unfiltered rows per oven (not documented
# anywhere the user pointed to - reverse-engineered from real data). 809 was
# user-provided and confirmed to isolate exactly the one real Started load
# that was live at the time; 808/810 were simply the only other values
# observed in the same real data and are recorded for reference, not because
# any caller needs them yet.
STATUS_PLANNED = "808"
STATUS_STARTED = "809"
STATUS_COMPLETED = "810"


def get_furnace_loads(workcenter_key, begin_date, end_date, active_only=True, status_key=None):
    """Recent furnace load cycles for one workcenter.

    workcenter_key: Plex's WorkcenterKey - see WORKCENTER_SMALL_OVEN /
      WORKCENTER_LARGE_OVEN above.
    begin_date, end_date: ISO 8601 strings, e.g. "2026-08-25T05:00:00.000Z".
      Per the user's own note captured alongside the original request: the
      begin date should be one day before "today" to reliably catch the
      currently-running cycle, which may have started the previous day.
    status_key: optional FurnaceLoadStatusKey (see STATUS_* above) to filter
      server-side. Narrowing to STATUS_STARTED specifically is dramatically
      faster - measured live 2026-08-27 at 1.9s for 1 row vs. 19.9s for 140
      unfiltered rows over the same window - because Plex does the filtering
      itself rather than this returning everything for client-side filtering.
      See get_current_loads() for the confirmed-vs-guess pattern this exists
      to support; most callers should use that rather than this directly.

    Returns the raw list of load records (each with CyclesData - temperature,
    ActualStartTime/ActualEndTime - and ContainersData - SerialNo, JobNo,
    Quantity per container in that load). sourceActionKey=19727 is Plex's own
    internal identifier for this search screen, carried over from the capture
    as-is rather than guessed at.
    """
    payload = {
        "ActiveOnly": active_only,
        "BeginDate": begin_date,
        "EndDate": end_date,
        "WorkcenterKey": str(workcenter_key),
    }
    if status_key is not None:
        payload["FurnaceLoadStatusKey"] = status_key
    resp = _post(
        "https://cloud.plex.com/ProductionTracking/FurnaceLoad/Search",
        params={"limit": "true", "sourceActionKey": "19727"},
        json=payload,
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("Data", {}).get("Rows", [])


def get_current_loads(workcenter_key, begin_date, end_date):
    """Best answer to "what load(s) are running in this oven right now?"

    Almost always exactly one. Confirmed live 2026-08-28: some Plex
    "programs" are a deliberate workaround letting certain parts run under
    either of two program numbers interchangeably (OperationCode "Aging
    Prog #002 OR #018", the same string _program_number() already treats as
    ambiguous) - Plex can have TWO loads simultaneously marked Started for
    the same oven in that case, and both are genuinely current. Picking
    just one (as this used to) silently drops real parts from the
    dashboard, so every confirmed-Started load is returned.

    Two-tier lookup, in order:

    1. CONFIRMED: ask Plex directly for loads with FurnaceLoadStatusKey ==
       STATUS_STARTED. If Plex has any, that is the operator having
       actually toggled "Started" on each of them - trust all of them
       outright, and this is also the fast path (see the latency note on
       get_furnace_loads' status_key).

    2. GUESS, if step 1 found nothing: the operator may simply not have
       marked anything Started yet even though a load is physically running.
       Falls back to an unfiltered search over the same window. Many older
       records (confirmed live 2026-08-27 - a load numbered far below the
       current ~28600s still turned up in a 2-day window search) carry a
       CyclesData entry that is entirely null - no ActualStartTime, no
       ActualEndTime, Temperature 0 - not a currently-running load, just an
       incompletely-logged historical one, and BeginDate/EndDate evidently
       does not filter these out by real occurrence time the way its name
       suggests. Those are excluded outright by requiring a real
       ActualStartTime. Among what remains, a load with no ActualEndTime yet
       is preferred (genuinely looks unfinished) over one with a real end
       time (definitely already over); within either group, the most
       recently started one wins. Only ever a single best guess here, even
       if the dual-program workaround is in play - guessing that BOTH of
       its loads are simultaneously active with no operator confirmation at
       all would compound one guess into two, not something to do
       speculatively. This is still a guess, not a confirmation - it is
       presented as one via the confirmed=False return value.

    Returns (list_of_load_dicts, confirmed: bool). ([], False) means
    neither tier found anything - genuinely no load data in this window.
    """
    started = get_furnace_loads(workcenter_key, begin_date, end_date, status_key=STATUS_STARTED)
    if started:
        return started, True

    all_loads = get_furnace_loads(workcenter_key, begin_date, end_date)

    def _start_time(load):
        cycles = load.get("CyclesData") or []
        return cycles[0].get("ActualStartTime") if cycles else None

    def _still_open(load):
        cycles = load.get("CyclesData") or []
        return bool(cycles) and cycles[0].get("ActualEndTime") is None

    candidates = [load for load in all_loads if _start_time(load)]
    if not candidates:
        return [], False

    # ISO 8601 UTC strings in a consistent format sort chronologically as
    # plain strings - no datetime parsing needed for "which is most recent."
    open_candidates = [load for load in candidates if _still_open(load)]
    pool = open_candidates or candidates
    best = max(pool, key=_start_time)
    return [best], False


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
