# Egress guard (optional)

A forward proxy (Squid) that the backend's outbound `httpx` requests
route through, so a request the backend makes to a URL it doesn't
control the DNS for can't be redirected to a private/loopback/
link-local address. This closes a gap the app-level SSRF guard
(`backend/app/url_safety.py`) can't: that guard checks a URL's
resolved IP once, then httpx does its own separate DNS lookup to
actually connect — a host whose DNS answer changes between those two
lookups can slip a private IP past the check. This proxy resolves and
connects in one step, so there's no second lookup to race.

## What it does and doesn't cover

- Covers every outbound HTTP(S) request the backend makes via
  `httpx` — RSS/Atom feed fetches, article/favicon/thumbnail
  fetches, YouTube channel resolution, the direct-mode Reddit client,
  podcast audio/transcript fetches, and any LLM provider call.
- Does **not** cover Postgres or Redis — those connect over raw TCP
  (`asyncpg`, `redis-py`), which don't consult `HTTP_PROXY`/
  `HTTPS_PROXY` at all, so this proxy is irrelevant to them either
  way (they're never routed through it, and don't need to be).
- Relies on `httpx`'s default `trust_env=True` behavior (every
  client in this codebase uses that default). If a future change
  explicitly sets `trust_env=False` or `proxy=`/`mounts=` on some
  client, that client silently stops going through this guard — it
  isn't enforced at the code level, only by convention.

## Enabling it

Off by default — this adds a new container and changes how the
backend reaches the internet, so it's opt-in rather than silently
turned on for existing deployments.

1. Add to `.env`:

   ```
   COMPOSE_PROFILES=hardened
   HTTP_PROXY=http://guard-proxy:3128
   HTTPS_PROXY=http://guard-proxy:3128
   ```

2. `docker compose up -d` (the profile brings up the `guard-proxy`
   service; the backend picks up the proxy env vars from `.env` on
   its next recreate).

3. If you run a self-hosted Reddit "Hydra" proxy
   (`REDDIT_HYDRA_URL`) as another container on this same Docker
   network, add its hostname to `egress-proxy/allowed-internal-hosts.txt`
   (one per line) before starting, or the guard will block the
   backend's connection to it.

## Verifying it's actually working

From the backend container, after enabling:

```
docker compose exec backend sh -c \
  'curl -sf -o /dev/null -w "%{http_code}\n" http://169.254.169.254/'
```

Should fail to connect (curl exits non-zero / times out) — that's
the guard denying a private/link-local destination. Then confirm the
app itself still works: check that new entries keep arriving for
your configured sources (`docker compose logs -f backend`), and that
Ollama (if configured) still responds — a broken egress rule fails
closed for the app's whole outbound path, not just the malicious
case, so this check matters as much as the deny check above.

## Testing notes

`squid.conf`'s actual behavior (not just its syntax) was verified
directly against a real Squid binary during development: private/
loopback/link-local destinations are denied — including over an
HTTPS `CONNECT` tunnel, not just plain HTTP — and the `host.docker.internal`
/ `allowed-internal-hosts.txt` exceptions are allowed through despite
resolving to a private address. The container build itself
(`docker build ./egress-proxy`) was **not** verified end-to-end in
the environment this was developed in (no Docker registry access
there); if something about the Alpine base image's packaging
surprises you, the two places most likely to need adjusting are the
`squid`/`proxy` user Alpine's `apk add squid` creates (the Dockerfile
creates its own idempotently, so this should be moot) and the
default `Safe_ports`/`SSL_ports` set if you need the backend to reach
a feed on a non-standard port.
