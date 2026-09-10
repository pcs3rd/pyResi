"""pyResi — a small client for the Resi Central API.

Covers what studio.resi.io's own web client does: log in, list encoders and
the "channels" derived from them, look up events (recorded or live
broadcasts), and read/write cue markers on an event's timeline.

This is unofficial and reverse-engineered (from HAR captures of
studio.resi.io and ResiClient's own Swift source) — not Resi's documentation.
Endpoints marked as gaps below are best-effort guesses; everything else has
been seen working against a real account.
"""

import json
import time
import uuid
from pathlib import Path

from requests import Session


class ResiAPIError(Exception):
    """A non-2xx response from the Resi API.

    No structured error envelope has been reverse-engineered yet — Resi's own
    client just surfaces the status code and raw body, so this does the same.
    """

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f'Resi API error {status_code}: {body[:500]}')


def _raise_for_status(resp):
    if not resp.ok:
        raise ResiAPIError(resp.status_code, resp.text)
    return resp


def _ensure_https(url):
    """Normalize a protocol-relative manifest URL (//resi.media/...) to https://.

    Resi's own web player builds these relative to the current page, the way
    a browser resolves them. There's no page here, so a bare //host/... must
    be given an explicit scheme before anything can request it.
    """
    if url and url.startswith('//'):
        return f'https:{url}'
    return url


# ---------- Base class, handles authentication ----------
class pyResi:
    API_URL = 'https://central.resi.io'
    SESSION_FILE = Path.home() / '.pyresi_session.json'

    def __init__(self, username=None, password=None, token=None):
        self.session = Session()
        self.username = username
        self.password = password
        self.token = token
        self.token_expires_at = 0
        self.customer_id = None

        if self.token is not None:
            # Caller handed us a token directly — trust it, skip everything else.
            self._apply_token(self.token)
        elif self._load_saved_session():
            # 1) Picked up a still-valid (or refreshable) session from disk.
            pass
        else:
            # 2) Nothing usable saved — do a full login.
            self._login(username, password)
            self._save_session()

        # ---------- Endpoint namespaces ----------
        self.encoders = _Encoders(self)
        self.channels = _Channels(self)
        self.events = _Events(self)
        self.cues = _Cues(self)

    # ---------- auth ----------

    def _login(self, username, password):
        login_resp = self.session.post(
            f'{self.API_URL}/api/v3/login?newToken=true',
            json={'userName': str(username), 'password': str(password)},
        )
        login_resp.raise_for_status()

        token_resp = self.session.post(
            f'{self.API_URL}/api/v3/auth/token',
            json={
                'username': str(username),
                'password': str(password),
                'grant_type': 'password_cookie',
            },
        )
        token_resp.raise_for_status()
        self._store_token_response(token_resp.json())

    def _refresh(self):
        """Silent reauth using only the refreshToken cookie — no password needed."""
        token_resp = self.session.post(
            f'{self.API_URL}/api/v3/auth/token',
            json={'grant_type': 'refresh_token_cookie'},
        )
        token_resp.raise_for_status()
        self._store_token_response(token_resp.json())

    def ensure_authenticated(self):
        """Call this before any API request — refreshes or re-logs-in as needed."""
        if time.time() < self.token_expires_at - 30:  # 30s safety margin
            return
        try:
            self._refresh()
        except Exception:
            if not (self.username and self.password):
                raise RuntimeError('Token expired and no credentials available to reauth')
            self._login(self.username, self.password)
        self._save_session()

    def _store_token_response(self, data):
        # Resi's actual field name for the access token hasn't been confirmed
        # from a real response body (see the API doc's Authentication section) —
        # try every plausible spelling so this survives whichever one it is.
        token = None
        for key in ('accessToken', 'access_token', 'token', 'authToken', 'bearerToken'):
            if data.get(key):
                token = data[key]
                break
        if not token:
            raise ValueError('Failed to obtain a Resi access token')

        expires_in = data.get('expiresIn') or data.get('expires_in') or 3600
        self.token_expires_at = time.time() + expires_in
        self._apply_token(token)

    def _apply_token(self, token):
        self.token = token
        self.session.headers.update({
            'Authorization': f'X-Bearer {token}',
            'Accept': 'application/json',
        })

    # ---------- persistence across invocations ----------

    def _save_session(self):
        data = {
            'token': self.token,
            'expires_at': self.token_expires_at,
            'cookies': self.session.cookies.get_dict(),
        }
        self.SESSION_FILE.write_text(json.dumps(data))

    def _load_saved_session(self):
        if not self.SESSION_FILE.exists():
            return False
        try:
            data = json.loads(self.SESSION_FILE.read_text())
            self.session.cookies.update(data.get('cookies', {}))
            self.token_expires_at = data.get('expires_at', 0)
            saved_token = data.get('token')
            if not saved_token:
                return False
            self._apply_token(saved_token)
            self.ensure_authenticated()  # refreshes if expired, using the cookie
            return True
        except Exception:
            return False

    # ---------- who am I ----------

    def whoami(self):
        """GET /api_v2.svc/users/me — the authenticated user; also caches
        customer_id, which every Events call needs."""
        resp = self.get('/api_v2.svc/users/me')
        _raise_for_status(resp)
        data = resp.json()
        if data.get('customerId'):
            self.customer_id = data['customerId']
        return data

    def _ensure_customer_id(self):
        if not self.customer_id:
            self.whoami()
        if not self.customer_id:
            raise RuntimeError(
                "Couldn't determine customerId — call whoami() yourself, "
                'or check that the account has one'
            )
        return self.customer_id

    # ---------- authenticated requests ----------

    @staticmethod
    def _tracking_headers():
        # Mirrors the headers Studio's own browser client sends on writes
        # (trackingid / x-request-id, same client-generated value in both).
        # Whether the server actually validates them is unconfirmed.
        tracking_id = f'STUDIO_{uuid.uuid4()}'
        return {'trackingid': tracking_id, 'x-request-id': tracking_id}

    def get(self, path, **kwargs):
        self.ensure_authenticated()
        return self.session.get(f'{self.API_URL}{path}', **kwargs)

    def post(self, path, **kwargs):
        self.ensure_authenticated()
        kwargs.setdefault('headers', {}).update(self._tracking_headers())
        return self.session.post(f'{self.API_URL}{path}', **kwargs)

    def patch(self, path, **kwargs):
        self.ensure_authenticated()
        kwargs.setdefault('headers', {}).update(self._tracking_headers())
        return self.session.patch(f'{self.API_URL}{path}', **kwargs)

    def delete(self, path, **kwargs):
        self.ensure_authenticated()
        kwargs.setdefault('headers', {}).update(self._tracking_headers())
        return self.session.delete(f'{self.API_URL}{path}', **kwargs)


# ---------- Encoders ----------
class _Encoders:
    """The physical/virtual encoders on the account."""

    def __init__(self, client):
        self._client = client

    def list(self):
        """GET /api_v2.svc/encoders?wide=true — all encoders. wide=true is what
        pulls in the nested streamProfile object that channel grouping needs."""
        resp = self._client.get('/api_v2.svc/encoders', params={'wide': 'true'})
        _raise_for_status(resp)
        return resp.json()

    def status(self, encoder_id):
        """GET /api_v2.svc/encoders/{encoderId}/status — an encoder's live status,
        including currentEventId (None when it isn't currently streaming)."""
        resp = self._client.get(f'/api_v2.svc/encoders/{encoder_id}/status')
        _raise_for_status(resp)
        return resp.json()


# ---------- Channels ----------
class _Channels:
    """Studio's UI calls these "channels", but there's no dedicated listing
    endpoint for them — they're just the distinct streamProfile objects
    nested in the encoder list, de-duplicated client-side."""

    def __init__(self, client):
        self._client = client

    def list(self):
        """Derived, not fetched: walk encoders, keep the first streamProfile
        seen per distinct uuid, sort by name."""
        seen = {}
        for encoder in self._client.encoders.list():
            profile = encoder.get('streamProfile')
            if not profile or not profile.get('uuid'):
                continue
            seen.setdefault(profile['uuid'], profile)
        return sorted(seen.values(), key=lambda p: (p.get('name') or '').lower())


# ---------- Events (videos) ----------
class _Events:
    """A Resi "event" is one recorded or in-progress broadcast."""

    def __init__(self, client):
        self._client = client

    def list(self):
        """GET /api/v3/customers/{customerId}/events — every event across every
        encoder on the account."""
        customer_id = self._client._ensure_customer_id()
        resp = self._client.get(f'/api/v3/customers/{customer_id}/events')
        _raise_for_status(resp)
        return resp.json()

    def get(self, event_id):
        """GET /api/v3/customers/{customerId}/events/{eventId}"""
        customer_id = self._client._ensure_customer_id()
        resp = self._client.get(f'/api/v3/customers/{customer_id}/events/{event_id}')
        _raise_for_status(resp)
        return resp.json()

    def for_encoder(self, encoder_id):
        """Convenience: this account's events filtered to one encoder, the way
        each picker in Studio filters the same full list client-side."""
        return [e for e in self.list() if e.get('encoderId') == encoder_id]

    def current_for_encoder(self, encoder_id):
        """Convenience: resolve a live encoder's currentEventId to a full event
        object. Returns None if the encoder isn't currently streaming."""
        status = self._client.encoders.status(encoder_id)
        event_id = status.get('currentEventId')
        if not event_id:
            return None
        return self.get(event_id)

    @staticmethod
    def hls_url(event):
        """event['hlsUrl'], normalized to an absolute https:// URL."""
        return _ensure_https(event.get('hlsUrl'))

    @staticmethod
    def dash_url(event):
        """event['cloudUrl'] (DASH manifest, same content as the HLS one), normalized."""
        return _ensure_https(event.get('cloudUrl'))


# ---------- Cues ----------
class _Cues:
    """Named markers on an event's timeline, keyed to the event's own
    streamProfile (called eventProfileId on the event object)."""

    def __init__(self, client):
        self._client = client

    def list(self, event_profile_id, event_id):
        """GET .../cues?canISetCues=1 — sorted by position. Lexicographic sort is
        safe here since position is zero-padded HH:MM:SS.mmm."""
        resp = self._client.get(
            f'/api_v2.svc/streamprofiles/{event_profile_id}/events/{event_id}/cues',
            params={'canISetCues': 1},
        )
        _raise_for_status(resp)
        return sorted(resp.json(), key=lambda c: c.get('position', ''))

    def create(self, event_profile_id, event_id, position, name, private_cue=True, user=None):
        """POST .../cues — creates a cue. The response is a bare 201 with an empty
        body; the server assigns the uuid silently and never hands it back, so
        this re-lists and matches on (position, name) to return the created cue.
        That match is best-effort — if another cue with the same position and
        name already exists, the wrong one could be returned."""
        body = {
            'position': position,
            'name': name,
            'privateCue': private_cue,
            'user': user or self._client.username or '',
        }
        resp = self._client.post(
            f'/api_v2.svc/streamprofiles/{event_profile_id}/events/{event_id}/cues',
            json=body,
        )
        _raise_for_status(resp)
        for cue in self.list(event_profile_id, event_id):
            if cue.get('position') == position and cue.get('name') == name:
                return cue
        return None

    def update(self, event_profile_id, event_id, cue_id, position, name, private_cue=True, user=None):
        """PATCH .../cues/{cueId} — edits a cue. Sends every field, not just the
        changed one: the one captured PATCH re-sent the full cue on a simple
        position move, so treat this as a full replace until proven otherwise."""
        body = {
            'position': position,
            'name': name,
            'privateCue': private_cue,
            'user': user or self._client.username or '',
        }
        resp = self._client.patch(
            f'/api_v2.svc/streamprofiles/{event_profile_id}/events/{event_id}/cues/{cue_id}',
            json=body,
        )
        _raise_for_status(resp)
        return True

    def delete(self, event_profile_id, event_id, cue_id):
        """DELETE .../cues/{cueId} — GAP: never captured in a HAR (Studio's cue
        editor was only observed creating and editing). This follows REST
        convention as a best guess; if the server rejects it, that's the
        confirmation this still needs."""
        resp = self._client.delete(
            f'/api_v2.svc/streamprofiles/{event_profile_id}/events/{event_id}/cues/{cue_id}'
        )
        _raise_for_status(resp)
        return True
