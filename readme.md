# pyResi

Unofficial Python client for the Resi Central API — the REST API behind
`studio.resi.io`. Reverse-engineered from HAR captures and ResiClient's own
Swift source, not Resi's documentation, so treat it accordingly.

## Install

```bash
uv sync
```

## Usage

```python
from pyResi import pyResi

client = pyResi(username="you@yourchurch.org", password="...")
# or: pyResi(token="already-have-a-bearer-token")

# Encoders / channels
encoders = client.encoders.list()
channels = client.channels.list()          # derived client-side, no dedicated endpoint

# Events (recorded or live broadcasts)
events = client.events.list()
live_event = client.events.current_for_encoder(encoders[0]["uuid"])
if live_event:
    print(client.events.hls_url(live_event))  # normalized to https://

# Cues (timeline markers)
event_profile_id = live_event["eventProfileId"]
event_id = live_event["uuid"]
cues = client.cues.list(event_profile_id, event_id)
new_cue = client.cues.create(event_profile_id, event_id, "00:00:00.000", "Stream Start")
client.cues.update(event_profile_id, event_id, new_cue["uuid"], "00:00:11.000", "Stream Start")
```

A session (token + cookies) is cached at `~/.pyresi_session.json` and reused
across runs; `client.ensure_authenticated()` refreshes or re-logs-in as
needed before every call, so you normally never call it yourself.

## Status

See the [Resi Central API reference](https://claude.ai/code/artifact/7ce65978-db35-4a80-bfdf-1c68b3137ec5)
for what's confirmed against a real account versus inferred/guessed —
notably, cue deletion (`client.cues.delete(...)`) is implemented against REST
convention but has never been confirmed working, since Studio's own UI was
never observed deleting a cue.
