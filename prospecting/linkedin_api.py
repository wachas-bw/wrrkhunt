"""Official LinkedIn OAuth and Comments API delivery.

This module never controls linkedin.com pages. It posts only through LinkedIn's
documented, permission-gated API and stops on any ambiguous external result.
"""
from __future__ import annotations

import getpass
import json
import random
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .config import (
    LINKEDIN_ACCESS_TOKEN_SERVICE,
    LINKEDIN_CLIENT_SECRET_SERVICE,
    LINKEDIN_REFRESH_TOKEN_SERVICE,
    keychain_get,
    keychain_set,
)
from .copy_engine import validate_stored_message
from .db import Database, StateError
from .util import (
    content_hash,
    in_business_window,
    iso,
    next_business_window,
    parse_iso,
    text_hash,
    utcnow,
)

OFFICIAL_API_MODE = "official_api"
AUTHORIZATION_ENDPOINT = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_ENDPOINT = "https://www.linkedin.com/oauth/v2/accessToken"
COMMENTS_ENDPOINT = "https://api.linkedin.com/rest/socialActions"
CURRENT_MEMBER_ENDPOINT = "https://api.linkedin.com/v2/me"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8766/callback"

_ACTOR_RE = re.compile(r"^urn:li:(person|organization):[A-Za-z0-9_-]+$")
_DIRECT_TARGET_RE = re.compile(
    r"urn:li:(activity|share|ugcpost):(\d{6,})", re.IGNORECASE,
)
_ACTIVITY_ID_RE = re.compile(r"(?:^|[-_:])activity[-_:](\d{6,})(?:$|[-_/?#])", re.IGNORECASE)
_VERSION_RE = re.compile(r"^20\d{4}$")


class LinkedInAPIStop(RuntimeError):
    """A fail-closed LinkedIn API setup or delivery error."""


class LinkedInHTTPError(LinkedInAPIStop):
    def __init__(self, message: str, *, status: int = 0, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def validate_actor_urn(value: str) -> str:
    value = (value or "").strip()
    if not _ACTOR_RE.fullmatch(value):
        raise LinkedInAPIStop(
            "actor URN must be urn:li:person:<id> or urn:li:organization:<id>"
        )
    return value


def required_scope(actor_urn: str) -> str:
    actor_urn = validate_actor_urn(actor_urn)
    return (
        "w_member_social_feed"
        if actor_urn.startswith("urn:li:person:")
        else "w_organization_social_feed"
    )


def _current_member(token: str) -> tuple[str, str]:
    """Resolve the member bound to the OAuth token using r_basicprofile."""
    request = urllib.request.Request(
        CURRENT_MEMBER_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        raise LinkedInAPIStop(
            f"could not verify the authorized LinkedIn member (HTTP {exc.code})"
            + (f": {detail}" if detail else "")
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LinkedInAPIStop(f"could not verify the authorized LinkedIn member: {exc}") from None
    member_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
    member_urn = f"urn:li:person:{member_id}"
    validate_actor_urn(member_urn)
    name = " ".join(filter(None, (
        str(payload.get("localizedFirstName") or "").strip(),
        str(payload.get("localizedLastName") or "").strip(),
    )))
    return member_urn, name


def target_urn_from_url(post_url: str) -> str:
    """Extract the API target URN from a user-supplied LinkedIn permalink."""
    decoded = urllib.parse.unquote(post_url or "")
    direct = _DIRECT_TARGET_RE.search(decoded)
    if direct:
        kind = direct.group(1).lower()
        canonical = {"activity": "activity", "share": "share", "ugcpost": "ugcPost"}[kind]
        return f"urn:li:{canonical}:{direct.group(2)}"
    activity = _ACTIVITY_ID_RE.search(decoded)
    return f"urn:li:activity:{activity.group(1)}" if activity else ""


def _valid_redirect_uri(value: str) -> str:
    value = (value or DEFAULT_REDIRECT_URI).strip()
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or not parsed.port
        or parsed.path != "/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise LinkedInAPIStop(
            "redirect URI must be an exact loopback URL such as " + DEFAULT_REDIRECT_URI
        )
    return value


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(2048).decode("utf-8", "replace")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error_description") or payload.get("error")
            if detail:
                return " ".join(str(detail).split())[:500]
        return " ".join(raw.split())[:500]
    except Exception:
        return ""


def _token_request(fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        raise LinkedInAPIStop(
            f"LinkedIn OAuth token exchange failed with HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LinkedInAPIStop(f"LinkedIn OAuth token exchange failed: {exc}") from None
    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        raise LinkedInAPIStop("LinkedIn OAuth response did not contain an access token")
    return payload


def _receive_authorization_code(auth_url: str, redirect_uri: str, expected_state: str,
                                timeout_seconds: int = 180) -> str:
    redirect = urllib.parse.urlsplit(redirect_uri)
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            current = urllib.parse.urlsplit(self.path)
            if current.path != redirect.path:
                self.send_error(404)
                return
            values = urllib.parse.parse_qs(current.query)
            returned_state = (values.get("state") or [""])[0]
            if not returned_state or not secrets.compare_digest(returned_state, expected_state):
                result["error"] = "OAuth state mismatch"
                status, message = 400, "Authorization rejected: state mismatch."
            elif values.get("error"):
                description = (values.get("error_description") or values.get("error") or [""])[0]
                result["error"] = " ".join(description.split())[:500] or "authorization denied"
                status, message = 400, "LinkedIn authorization was not completed."
            else:
                result["code"] = (values.get("code") or [""])[0]
                status, message = 200, "LinkedIn authorization received. You can close this tab."
            body = (
                "<!doctype html><meta charset=utf-8><title>wrrkhunt LinkedIn setup</title>"
                f"<p>{message}</p>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", int(redirect.port)), CallbackHandler)
    deadline = time.monotonic() + timeout_seconds
    try:
        if not webbrowser.open(auth_url, new=2):
            raise LinkedInAPIStop("could not open the LinkedIn OAuth consent page")
        while not result and time.monotonic() < deadline:
            server.timeout = min(1.0, max(0.1, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    if result.get("error"):
        raise LinkedInAPIStop("LinkedIn OAuth failed: " + result["error"])
    if not result.get("code"):
        raise LinkedInAPIStop("LinkedIn OAuth timed out before the loopback callback arrived")
    return result["code"]


def _store_token_payload(db: Database, client_id: str, payload: dict[str, Any], *,
                         preserve_refresh: bool = False) -> str:
    access_token = str(payload["access_token"]).strip()
    try:
        expires_in = max(1, int(payload.get("expires_in", 0)))
    except (TypeError, ValueError):
        expires_in = 0
    if not expires_in:
        raise LinkedInAPIStop("LinkedIn OAuth response did not provide a valid token lifetime")
    keychain_set(access_token, LINKEDIN_ACCESS_TOKEN_SERVICE, client_id)
    expires_at = iso(utcnow() + timedelta(seconds=expires_in))
    db.set_setting("linkedin_api_token_expires_at", expires_at)

    refresh_token = str(payload.get("refresh_token") or "").strip()
    if refresh_token:
        keychain_set(refresh_token, LINKEDIN_REFRESH_TOKEN_SERVICE, client_id)
        db.set_setting("linkedin_api_refresh_available", True)
        try:
            refresh_seconds = max(1, int(payload.get("refresh_token_expires_in", 0)))
        except (TypeError, ValueError):
            refresh_seconds = 0
        db.set_setting(
            "linkedin_api_refresh_expires_at",
            iso(utcnow() + timedelta(seconds=refresh_seconds)) if refresh_seconds else "",
        )
    elif not preserve_refresh:
        # Do not reuse a refresh token left in Keychain by an older authorization.
        db.set_setting("linkedin_api_refresh_available", False)
        db.set_setting("linkedin_api_refresh_expires_at", "")
    return expires_at


def setup_linkedin_api(db: Database, *, client_id: str, actor_urn: str,
                       redirect_uri: str = DEFAULT_REDIRECT_URI) -> dict[str, str]:
    """Run the official three-legged OAuth flow and enable API delivery."""
    client_id = (client_id or "").strip()
    if not client_id or len(client_id) > 200 or any(ch.isspace() for ch in client_id):
        raise LinkedInAPIStop("a valid LinkedIn Developer app Client ID is required")
    requested_actor = (actor_urn or "me").strip()
    if requested_actor.lower() == "me":
        actor_urn = ""
        scope = "w_member_social_feed"
    else:
        actor_urn = validate_actor_urn(requested_actor)
        scope = required_scope(actor_urn)
    redirect_uri = _valid_redirect_uri(redirect_uri)
    oauth_scopes = f"r_basicprofile {scope}"

    db.set_setting("linkedin_posting_mode", OFFICIAL_API_MODE)
    db.set_channel(
        "linkedin", paused=True, reason="LinkedIn official API OAuth setup in progress",
        credential_status="pending",
    )
    try:
        client_secret = keychain_get(LINKEDIN_CLIENT_SECRET_SERVICE, client_id)
        if not client_secret:
            client_secret = getpass.getpass("LinkedIn Developer app Client Secret: ").strip()
            if not client_secret:
                raise LinkedInAPIStop("LinkedIn Developer app Client Secret is required")
            keychain_set(client_secret, LINKEDIN_CLIENT_SECRET_SERVICE, client_id)

        state = secrets.token_urlsafe(32)
        auth_url = AUTHORIZATION_ENDPOINT + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": oauth_scopes,
        })
        code = _receive_authorization_code(auth_url, redirect_uri, state)
        payload = _token_request({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        })
        returned_scopes = set(filter(None, re.split(r"[,\s]+", str(payload.get("scope") or ""))))
        if returned_scopes and not {"r_basicprofile", scope}.issubset(returned_scopes):
            raise LinkedInAPIStop(
                f"LinkedIn did not grant both r_basicprofile and {scope} permissions"
            )
        authorized_member_urn, authorized_member_name = _current_member(
            str(payload["access_token"]).strip()
        )
        if actor_urn.startswith("urn:li:person:") and actor_urn != authorized_member_urn:
            raise LinkedInAPIStop(
                "configured person actor does not match the member who approved OAuth"
            )
        if not actor_urn:
            actor_urn = authorized_member_urn
        expires_at = _store_token_payload(db, client_id, payload)
        db.set_setting("linkedin_api_client_id", client_id)
        db.set_setting("linkedin_api_actor_urn", actor_urn)
        db.set_setting("linkedin_api_authorized_member_urn", authorized_member_urn)
        db.set_setting("linkedin_api_authorized_member_name", authorized_member_name)
        db.set_setting("linkedin_api_redirect_uri", redirect_uri)
        db.set_setting("linkedin_api_scope", scope)
        db.set_channel(
            "linkedin", paused=False,
            reason="official LinkedIn Comments API enabled; approval gate remains active",
            credential_status="healthy",
        )
        return {
            "mode": OFFICIAL_API_MODE,
            "actor_urn": actor_urn,
            "authorized_member": authorized_member_name or authorized_member_urn,
            "scope": scope,
            "token_expires_at": expires_at,
        }
    except Exception as exc:
        reason = str(exc) if isinstance(exc, LinkedInAPIStop) else f"LinkedIn API setup failed: {exc}"
        db.set_channel("linkedin", paused=True, reason=reason, credential_status="failed")
        if isinstance(exc, LinkedInAPIStop):
            raise
        raise LinkedInAPIStop(reason) from None


def _access_token(db: Database, settings: dict[str, Any]) -> str:
    client_id = str(settings.get("linkedin_api_client_id") or "").strip()
    if not client_id:
        raise LinkedInAPIStop("LinkedIn API Client ID is not configured")
    token = keychain_get(LINKEDIN_ACCESS_TOKEN_SERVICE, client_id)
    expires_at = parse_iso(str(settings.get("linkedin_api_token_expires_at") or ""))
    if token and expires_at and expires_at > utcnow() + timedelta(minutes=5):
        return token

    refresh_available = bool(settings.get("linkedin_api_refresh_available", False))
    refresh_token = (
        keychain_get(LINKEDIN_REFRESH_TOKEN_SERVICE, client_id) if refresh_available else ""
    )
    refresh_expires = parse_iso(str(settings.get("linkedin_api_refresh_expires_at") or ""))
    if not refresh_token or (refresh_expires and refresh_expires <= utcnow()):
        raise LinkedInAPIStop("LinkedIn access token expired; run setup linkedin-api again")
    client_secret = keychain_get(LINKEDIN_CLIENT_SECRET_SERVICE, client_id)
    if not client_secret:
        raise LinkedInAPIStop("LinkedIn client secret is missing from macOS Keychain")
    payload = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    _store_token_payload(db, client_id, payload, preserve_refresh=True)
    return str(payload["access_token"]).strip()


def linkedin_api_health(db: Database) -> tuple[bool, str]:
    state = db.channel("linkedin")
    if state["emergency_stop"]:
        return False, "emergency stop active"
    if state["paused"]:
        return False, state["reason"] or "LinkedIn channel paused"
    settings = db.settings()
    try:
        actor_urn = validate_actor_urn(str(settings.get("linkedin_api_actor_urn") or ""))
        authorized_member_urn = validate_actor_urn(
            str(settings.get("linkedin_api_authorized_member_urn") or "")
        )
        if not authorized_member_urn.startswith("urn:li:person:"):
            raise LinkedInAPIStop("authorized OAuth identity must be a LinkedIn member")
        if actor_urn.startswith("urn:li:person:") and actor_urn != authorized_member_urn:
            raise LinkedInAPIStop("configured person actor does not match the OAuth member")
        expected_scope = required_scope(actor_urn)
        if settings.get("linkedin_api_scope") != expected_scope:
            raise LinkedInAPIStop(f"required LinkedIn scope {expected_scope} is not configured")
        version = str(settings.get("linkedin_api_version") or "")
        if not _VERSION_RE.fullmatch(version):
            raise LinkedInAPIStop("LinkedIn API version must use YYYYMM format")
        _access_token(db, settings)
    except LinkedInAPIStop as exc:
        db.set_channel("linkedin", paused=True, reason=str(exc), credential_status="failed")
        return False, str(exc)
    detail = f"official Comments API ready for {actor_urn} using version {version}"
    db.set_channel("linkedin", paused=False, reason=detail, credential_status="healthy")
    return True, detail


def _create_comment(token: str, version: str, actor_urn: str,
                    target_urn: str, comment: str) -> str:
    encoded_target = urllib.parse.quote(target_urn, safe="")
    payload = json.dumps({
        "actor": actor_urn,
        "object": target_urn,
        "message": {"text": comment},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{COMMENTS_ENDPOINT}/{encoded_target}/comments",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            raw = response.read().decode("utf-8", "replace").strip()
            header_id = str(response.headers.get("x-restli-id") or "").strip()
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        raise LinkedInHTTPError(
            f"LinkedIn Comments API HTTP {exc.code}" + (f": {detail}" if detail else ""),
            status=int(exc.code), uncertain=False,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LinkedInHTTPError(
            f"uncertain LinkedIn API submission state: {exc}", uncertain=True,
        ) from None
    if status < 200 or status >= 300:
        raise LinkedInHTTPError(f"LinkedIn Comments API returned HTTP {status}", status=status)
    body_id = ""
    returned_object = ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                body_id = str(parsed.get("commentUrn") or parsed.get("id") or "").strip()
                returned_actor = str(parsed.get("actor") or "").strip()
                returned_object = str(parsed.get("object") or "").strip()
                returned_message = parsed.get("message")
                returned_text = (
                    str(returned_message.get("text") or "")
                    if isinstance(returned_message, dict) else ""
                )
                if returned_actor and returned_actor != actor_urn:
                    raise LinkedInHTTPError(
                        "LinkedIn response actor did not match the approved OAuth actor",
                        status=status, uncertain=True,
                    )
                supported_object = re.fullmatch(
                    r"urn:li:(?:activity|share|ugcPost):\d{6,}", returned_object,
                ) if returned_object else None
                if returned_object and not supported_object:
                    raise LinkedInHTTPError(
                        "LinkedIn response did not contain a supported post object URN",
                        status=status, uncertain=True,
                    )
                if (
                    target_urn.startswith("urn:li:activity:")
                    and returned_object
                    and returned_object != target_urn
                ):
                    raise LinkedInHTTPError(
                        "LinkedIn response activity did not match the approved post target",
                        status=status, uncertain=True,
                    )
                if returned_text and returned_text != comment:
                    raise LinkedInHTTPError(
                        "LinkedIn response text did not match the approved comment",
                        status=status, uncertain=True,
                    )
        except json.JSONDecodeError:
            pass
    external_id = header_id or body_id
    if not external_id:
        raise LinkedInHTTPError(
            "LinkedIn accepted the request without a definitive comment ID; submission state is uncertain",
            status=status, uncertain=True,
        )
    if body_id.startswith("urn:li:comment:"):
        return body_id
    if external_id.isdigit() and returned_object:
        return f"urn:li:comment:({returned_object},{external_id})"
    return external_id


def _exact_hash(row: dict[str, Any]) -> bool:
    expected = content_hash(
        row["channel"], row["kind"], row["to_address"], row["subject"],
        row["body"], json.loads(row["evidence_ids_json"]),
    )
    return expected == row["content_hash"] == row["approved_hash"]


def _fail_before_attempt(db: Database, message_id: int, reason: str) -> None:
    db.mark_failed(message_id, reason, count_attempt=False)


def post_due_api(db: Database, limit: int = 5) -> dict[str, Any]:
    """Post due, approved comments through LinkedIn's official Comments API."""
    state = db.channel("linkedin")
    if state["paused"] or state["emergency_stop"]:
        return {"posted": 0, "blocked": state["reason"] or "channel paused"}
    settings = db.settings()
    if settings.get("linkedin_posting_mode") != OFFICIAL_API_MODE:
        return {"posted": 0, "blocked": "official LinkedIn API mode is not enabled"}
    try:
        actor_urn = validate_actor_urn(str(settings.get("linkedin_api_actor_urn") or ""))
        authorized_member_urn = validate_actor_urn(
            str(settings.get("linkedin_api_authorized_member_urn") or "")
        )
        if not authorized_member_urn.startswith("urn:li:person:"):
            raise LinkedInAPIStop("authorized OAuth identity must be a LinkedIn member")
        if actor_urn.startswith("urn:li:person:") and actor_urn != authorized_member_urn:
            raise LinkedInAPIStop("configured person actor does not match the OAuth member")
        scope = required_scope(actor_urn)
        if settings.get("linkedin_api_scope") != scope:
            raise LinkedInAPIStop(f"required LinkedIn scope {scope} is not configured")
        version = str(settings.get("linkedin_api_version") or "")
        if not _VERSION_RE.fullmatch(version):
            raise LinkedInAPIStop("LinkedIn API version must use YYYYMM format")
        token = _access_token(db, settings)
    except LinkedInAPIStop as exc:
        db.set_channel("linkedin", paused=True, reason=str(exc), credential_status="failed")
        return {"posted": 0, "blocked": str(exc)}

    rows = db.rows(
        "SELECT m.*,p.post_url,p.author_url,p.published_at,p.market,p.text AS post_text,"
        "p.text_hash AS post_text_hash,p.status AS post_status,sr.source AS post_source "
        "FROM messages m JOIN posts p ON p.id=m.post_id "
        "LEFT JOIN source_runs sr ON sr.id=p.source_run_id "
        "WHERE m.channel='linkedin' AND m.kind='comment' AND m.status='scheduled' "
        "AND m.scheduled_for<=? ORDER BY m.scheduled_for LIMIT ?",
        (iso(), limit),
    )
    stale = next((row for row in rows if int(row["attempt_count"]) > 0), None)
    if stale:
        reason = "a prior LinkedIn API attempt ended without a definitive recorded outcome"
        db.mark_failed(stale["id"], reason, count_attempt=False)
        db.set_channel("linkedin", paused=True, reason=reason)
        return {"posted": 0, "failed": 1, "blocked": reason}

    posted = failed = 0
    latest = db.row(
        "SELECT MAX(updated_at) AS latest FROM messages "
        "WHERE channel='linkedin' AND attempt_count>0"
    )
    pacing_cursor = parse_iso(latest["latest"] if latest else None)
    rng = random.SystemRandom()
    for raw in rows:
        row = dict(raw)
        reason = ""
        lint_errors = validate_stored_message(db, int(row["id"]))
        if lint_errors:
            reason = "; ".join(lint_errors)
        elif not _exact_hash(row):
            reason = "immutable approved comment hash mismatch"
        elif row["post_source"] != "manual_linkedin":
            reason = "only posts supplied by the local user are eligible for API comments"
        elif not row["author_url"] or db.is_suppressed("linkedin", row["author_url"]):
            reason = "LinkedIn author is missing or suppressed"
        elif text_hash(row["post_text"]) != row["post_text_hash"]:
            reason = "stored LinkedIn post-text hash mismatch"
        published_at = parse_iso(row["published_at"])
        if not reason and (
            not published_at
            or published_at < utcnow() - timedelta(hours=int(settings["post_max_age_hours"]))
        ):
            reason = "LinkedIn post is no longer fresh enough for this queue"
        target_urn = target_urn_from_url(row["post_url"])
        if not reason and not target_urn:
            reason = "LinkedIn post URL does not expose a supported activity/share URN"
        duplicate = db.row(
            "SELECT 1 FROM messages old JOIN posts p ON p.id=old.post_id "
            "WHERE p.post_url=? AND old.id!=? AND old.status='posted' LIMIT 1",
            (row["post_url"], row["id"]),
        )
        if not reason and duplicate:
            reason = "this LinkedIn post already has a recorded comment"
        author_duplicate = db.row(
            "SELECT 1 FROM messages old JOIN posts p ON p.id=old.post_id "
            "WHERE p.author_url=? AND old.id!=? AND old.status='posted' AND old.sent_at>=? LIMIT 1",
            (row["author_url"], row["id"],
             iso(utcnow() - timedelta(days=int(settings["author_cooldown_days"])))),
        )
        if not reason and author_duplicate:
            reason = f"{int(settings['author_cooldown_days'])}-day author cooldown would be violated"
        if reason:
            _fail_before_attempt(db, int(row["id"]), reason)
            failed += 1
            continue
        if not in_business_window(
            utcnow(), row["market"], settings["market_policies"],
            int(settings["business_hour_start"]), int(settings["business_hour_end"]),
        ):
            continue
        now = utcnow()
        pacing_min = int(settings["linkedin_pacing_min_minutes"])
        if pacing_cursor and now < pacing_cursor + timedelta(minutes=pacing_min):
            when = next_business_window(
                pacing_cursor + timedelta(minutes=rng.randint(
                    pacing_min, int(settings["linkedin_pacing_max_minutes"]),
                )), row["market"],
                settings["market_policies"], int(settings["business_hour_start"]),
                int(settings["business_hour_end"]),
            )
            db.reschedule(int(row["id"]), iso(when), "rescheduled to preserve actual API pacing")
            pacing_cursor = when
            continue
        try:
            db.reserve_daily_action(
                "linkedin", "comment_attempt", iso()[:10], int(settings["linkedin_daily_cap"]),
            )
            db.reserve_message_attempt(int(row["id"]))
        except StateError as exc:
            return {"posted": posted, "failed": failed, "due": len(rows), "blocked": str(exc)}
        pacing_cursor = utcnow()
        try:
            external_id = _create_comment(
                token, version, actor_urn, target_urn, row["body"],
            )
        except LinkedInHTTPError as exc:
            db.mark_failed(int(row["id"]), str(exc), count_attempt=False)
            credential = "failed" if exc.status in {401, 403} else "quota" if exc.status == 429 else "unknown"
            db.set_channel(
                "linkedin", paused=True, reason=str(exc), credential_status=credential,
            )
            failed += 1
            break
        except Exception as exc:
            reason = f"uncertain LinkedIn API submission state: {exc}"
            db.mark_failed(int(row["id"]), reason, count_attempt=False)
            db.set_channel("linkedin", paused=True, reason=reason, credential_status="unknown")
            failed += 1
            break
        try:
            db.mark_delivered(int(row["id"]), external_id, posted=True)
            db.record_event(
                "linkedin", "official_api_posted", message_id=int(row["id"]),
                external_id=external_id,
                details={
                    "actor_urn": actor_urn,
                    "target_urn": target_urn,
                    "content_hash": row["content_hash"],
                    "post_text_hash": row["post_text_hash"],
                    "api_version": version,
                },
            )
            with db.transaction(immediate=True) as conn:
                conn.execute("UPDATE posts SET status='posted' WHERE id=?", (row["post_id"],))
        except Exception as exc:
            reason = f"LinkedIn posted but local delivery recording is uncertain: {exc}"
            db.set_channel("linkedin", paused=True, reason=reason, credential_status="unknown")
            return {"posted": posted, "failed": failed + 1, "due": len(rows), "blocked": reason}
        posted += 1
    return {"posted": posted, "failed": failed, "due": len(rows)}
