# HASP Messenger

A small web app for composing and pushing overlay messages to
[OpenHASP](https://www.openhasp.com/) plates over MQTT, with a live preview, a
server-proxied plate snapshot, per-plate templates, and Cloudflare Zero Trust
SSO. Built with FastAPI + vanilla JS, deployed as a systemd service on a
Raspberry Pi behind a Cloudflare tunnel.

The code itself is Python, but **the authentication and Cloudflare-policy
integration patterns documented below are language-agnostic** — they're
described in terms of HTTP requests, headers, and JSON payloads so they can
be lifted into a Node.js / Go / anything app without changes.

---

## What it does

- WYSIWYG editor with a live, pixel-accurate preview of the plate's overlay.
- Multi-plate: one MQTT broker, many plates, each with its own topic prefix,
  pixel dimensions, overlay id, IP, and per-plate templates.
- Server-side snapshot proxy: clients view plate screenshots through the
  server, so only the server needs LAN reach to the plate.
- MQTT control topic: external publishers can trigger a saved template by
  publishing to `hasp-messenger/<plate-slug>/<template-slug>`.
- Cloudflare Zero Trust + Google SSO with automatic local-user provisioning,
  per-resource permissions, and admin-driven CF Access policy sync.

---

## Architecture

```
Browser  ─►  Cloudflare Edge  ─►  CF Tunnel  ─►  FastAPI (uvicorn, port 8081)
                │ Google SSO            │              │
                │ Access policy         │              ├─► MQTT broker (TLS)
                │ Adds Cf-Access-*      │              │
                │ headers               │              ├─► Plate HTTP /screenshot
                                                       │   (proxied to clients)
                                                       │
                                                       └─► Cloudflare API
                                                           (policy sync)
                                                           
LAN clients ─►  direct to FastAPI :8081  (no CF headers → admin bypass)
```

---

## Cloudflare Access integration

This is the part most worth copying into another app. The goals:

1. **Single sign-on** — don't make users log in twice (once to CF Access, once
   to the app). The app trusts CF for who they are.
2. **No 403 walls** for legitimate-but-unprovisioned users. New users land on
   a "waiting for approval" screen, admins see them in a queue.
3. **Bidirectional sync** — when an admin adds or removes a user in the app,
   the email is also added to / removed from the Cloudflare Access policy
   that gates the app, so admins control everything from one place.

### The headers Cloudflare Access sends

When a request comes through a CF tunnel **and** the tunnel hostname is
protected by a CF Access application, Cloudflare injects two headers before
the request reaches your origin:

| Header | Value |
| --- | --- |
| `Cf-Access-Authenticated-User-Email` | The Google-authenticated email of the requester. Always lowercase. |
| `Cf-Access-Jwt-Assertion` | A signed JWT with `email`, `aud` (your Access app's audience tag), `iss` (`https://<team>.cloudflareaccess.com`), expiry, etc. |

Trust model options:

- **Trust the email header** (what this app does) — simplest. Relies on
  Cloudflare being the only thing that can add the header, which is true if
  your origin only receives traffic via the tunnel.
- **Verify the JWT** against CF's JWKS at
  `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`. Pin the
  expected `aud` (Application Audience tag, found on the Access app page).
  Required if the origin is also reachable outside the tunnel — otherwise a
  hostile client could spoof the header.

In this app, the LAN is trusted (the Pi is firewalled, not internet-exposed)
so a missing header is treated as an admin LAN bypass rather than a denial.

### Resolver pseudocode

```
function resolveUser(request):
    email = request.headers["Cf-Access-Authenticated-User-Email"]?.toLowerCase()

    if not email:
        # Direct LAN access — no CF in front, full admin bypass.
        return { email: "", isAdmin: true, isLan: true, allowedResources: ALL }

    existing = usersStore.get(email)
    if existing:
        return {
            email,
            isAdmin: existing.isAdmin,
            isLan: false,
            allowedResources: existing.isAdmin ? ALL : existing.allowedResourceIds,
        }

    # Unknown email path
    if usersStore.count() == 0:
        # First ever CF login bootstraps as admin
        user = { email, isAdmin: true, allowedResources: ALL }
        usersStore.upsert(user)
        return user

    # Otherwise: auto-add as pending so admins see them in the queue
    pending = { email, isAdmin: false, allowedResources: [], pending: true }
    usersStore.upsert(pending)
    return pending  # caller decides UI: show "waiting" screen
```

### Lifecycle of a new user

```
1. User clicks the public CF URL → CF Access redirects to Google login.
2. After Google auth, CF Access checks the Access policy.
   • If their email is not in the policy's include list → CF blocks with
     "You don't have access" page. App is never reached.
   • If it is in the policy → CF tunnel forwards the request with the
     Cf-Access-Authenticated-User-Email header.
3. The app's resolver sees the email:
   • Known + provisioned → normal UI.
   • Known + pending → "waiting for approval" screen.
   • Unknown + no users exist → auto-promoted to admin.
   • Unknown + users exist → auto-added as pending, "waiting" screen.
4. Admin opens the Users page, sees the pending user at the top with a
   yellow badge, ticks the resources they should access. The pending flag
   clears on the first admin edit.
```

### Auto cache-busting

CF aggressively caches static assets. Without versioned URLs you can ship a
JS update and CF will keep serving the old one until the TTL expires.

Two layers of defence:

1. `Cache-Control: no-store, must-revalidate` middleware on `/` and `/static/*`.
2. Asset URLs in the served HTML get a `?v=<build-id>` stamp where
   `build-id = max(mtime(index.html, app.js, style.css))`. The server
   rewrites the bare URLs in `index.html` on each request, so the moment any
   static file is touched, the URL changes and CF/browser cache misses.

---

## Cloudflare API sync — policy management

When an admin adds or removes a user in the app, the same change is
mirrored to the email-include list of a Cloudflare Access policy.

### What you need

| Thing | Where to get it |
| --- | --- |
| API token | CF dashboard → **Manage Account → API Tokens → Create Token**. Permission required: **Account → Access: Apps and Policies → Edit**. Restrict to a specific account for safety. The `cfat_` token format is fine. |
| Account ID | Bottom-right of any zone page in CF dashboard. |
| Application name *or* UUID | The name of the Access application protecting your tunnel hostname. |
| Policy name *or* UUID | The name of the policy within that application whose include list you want to manage. |

Resolve names → UUIDs once and cache them locally (this app stores them in
`data/config.json`); invalidate the cache if the lookup fails so the next
call re-resolves.

### Two policy types — IMPORTANT

CF Access has **two distinct kinds of policies** with different endpoints:

| Policy kind | Lives under | List | Get | Update |
| --- | --- | --- | --- | --- |
| App-scoped (legacy) | A specific Access app | `GET /accounts/{acct}/access/apps/{app_uid}/policies` | `GET .../apps/{app_uid}/policies/{policy_uid}` | `PUT .../apps/{app_uid}/policies/{policy_uid}` |
| Reusable (newer) | Account scope, attached to one or more apps | `GET /accounts/{acct}/access/policies` | `GET /accounts/{acct}/access/policies/{policy_uid}` | `PUT /accounts/{acct}/access/policies/{policy_uid}` |

Indicators that a policy is reusable: the response includes `"reusable": true`,
and the policy is listed under the account-scoped endpoint. Trying to PUT a
reusable policy through the app-scoped endpoint returns:

```
12130: access.api.error.invalid_request:
can not update reusable policies through this endpoint
```

**Robust strategy** (what this app does): when looking up a policy by name,
try the app-scoped list first; if no match, fall back to the account-scoped
reusable list. When fetching the policy itself or doing PUTs, derive the
path from the `reusable` flag on the policy you just fetched.

### Auth header

All endpoints take the same Bearer header:

```
Authorization: Bearer <api_token>
```

There is no separate `verify` endpoint for `cfat_` (Account-Owned) tokens —
their `/user/tokens/verify` returns 401 even when the token works. To
sanity-check a token, just call a real endpoint like
`GET /accounts/{acct}/access/apps`.

### Policy include rules

The fields we care about live in `policy.include` (a JSON array of rule
objects). For email-based rules:

```json
{ "email": { "email": "alice@example.com" } }
```

Other rule shapes exist (`email_domain`, `everyone`, `service_token`, `group`,
`ip`, etc.) — preserve them on round-trip. Only modify the `email` rules.

### Adding an email

```
1. GET the current policy (using the correct endpoint per kind).
2. If any include rule already has this email (case-insensitive), return ok.
3. Append { "email": { "email": "<lowercase>" } } to include[].
4. PUT the policy back. IMPORTANT: CF requires you to echo the full policy
   body minus auto-generated fields. Strip these before PUT:
       id, created_at, updated_at, reusable
   Keep everything else (name, decision, include, exclude, require,
   session_duration, …) exactly as you received it.
```

### Removing an email

```
1. GET the current policy.
2. Filter include[] removing any { email: { email: <target lowercase> } }.
3. PUT the policy back (same shape constraints as add).
```

### Sync from CF → local

For a "Sync users from Cloudflare" button — pull the policy's emails and
ensure each is a known local user (as **pending** if they're new). Don't
touch existing user records on sync; the local store is the source of
truth for permissions, CF is the source of truth for who can authenticate
at all.

### Error handling

Treat the CF call as **best-effort** by default — write the local user
change regardless of CF success, surface the CF error to the admin (we
return a 502 with both "local saved" and the CF detail in the message).
This way a transient CF API outage doesn't block local admin work, and
the admin can retry the sync once CF is back.

If you'd rather it be **strict** (CF must succeed or local rolls back),
that's a one-line policy change in the user create/delete handler.

---

## User permission model

A minimal sketch — adapt to whatever resource type your app cares about
(plates here, projects/sites/tenants in yours).

### User record (stored locally, JSON or DB)

```json
{
  "email": "alice@example.com",
  "name": "alice",
  "is_admin": false,
  "allowed_resource_ids": ["plate-id-1", "plate-id-2"],
  "pending": false,
  "created_at": 1779665350.0,
  "updated_at": 1779665350.0
}
```

### Rules of thumb

- **`is_admin`** users implicitly access every resource; you never need to
  read `allowed_resource_ids` for them.
- **`pending`** is automatically `true` for users auto-created by the
  resolver; it clears the first time an admin updates the record (any
  field). It's purely a UI signal for "needs attention".
- **LAN bypass** users (no `Cf-Access-*` header) get an empty email and
  are treated as ephemeral admins — they don't have a stored user record,
  so they can't be demoted.
- The user's email is the primary key. Lowercase everything on the way in
  (header, store, API path).

### Endpoint shape

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `GET /api/me` | any authenticated | Returns `{email, is_admin, is_lan, pending, allowed_resources}`. Frontend uses this to decide what UI to show. |
| `GET /api/users` | admin | List all stored users. |
| `PUT /api/users/{email}` | admin | Upsert. On *create*, mirror to CF policy. Always clear `pending`. |
| `DELETE /api/users/{email}` | admin | Delete locally. Mirror removal to CF policy. |
| `POST /api/cloudflare/test` | admin | Resolve UUIDs and fetch the policy. Hint loudly if creds verify but `enabled` is off. |
| `POST /api/cloudflare/sync` | admin | Pull policy emails → mark unknowns as pending. |

### Frontend states

When `/api/me` returns, the frontend has three branches:

1. `is_admin || allowed_resources.length > 0` → normal UI.
2. `pending` → "waiting for approval" page with the user's email.
3. Not admin, not pending, no resources → "no access — contact your admin"
   page (this is the "admin explicitly granted nothing" state).

---

## Layout of this repo

```
app/
  main.py          FastAPI app, route handlers, lifespan
  models.py        Pydantic models (User, Plate, CloudflareConfig, …)
  auth.py          The header-trust resolver + admin dependency
  cloudflare.py    CF Access policy sync (the section above in code)
  users.py         data/users.json read/write
  config.py        data/config.json read/write, migrations
  storage.py       data/templates.json read/write
  mqtt_client.py   Paho MQTT client + control-topic dispatcher
  ws_manager.py    WebSocket broadcast manager with per-user filtering

static/
  index.html       single-page app
  app.js           vanilla JS, no build step
  style.css

data/              runtime state — NOT in git
  config.json      broker + plates + CF section
  users.json       authenticated user records
  templates.json   saved per-plate templates

hasp-messenger.service   systemd unit
requirements.txt
run.py                   dev entry point (uvicorn binds 0.0.0.0:8000)
```

---

## Running locally

```
python -m venv .venv
. .venv/bin/activate         # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python run.py
```

Open <http://localhost:8000/>.

LAN-direct access is admin by default (no CF header), so you can configure
the broker / plates / Cloudflare section without setting up the tunnel
first. The CF section is wired up but inert until you fill it and tick
**Mirror user add/delete to a Cloudflare Access policy**.

## Deploying

Behind a CF tunnel pointed at the chosen port. Systemd unit
(`hasp-messenger.service`) runs uvicorn directly:

```
ExecStart=/home/pi/hasp-messenger/.venv/bin/uvicorn \
    app.main:app --host 0.0.0.0 --port 8081
```

The CF Access application on the tunnel hostname is what gates auth.
The CF Access **policy** managed by this app is the include list of
allowed users.
