# mealie-mcp

An [MCP](https://modelcontextprotocol.io) server that lets **Claude**, **ChatGPT** (or any remote-MCP client) read and add recipes in a
self-hosted [Mealie](https://mealie.io) library, with built-in **HelloFresh UK** lookup.

Send Claude or ChatGPT photos of HelloFresh recipe cards and say *"Add these to my Mealie"*. The assistant reads each card, the server finds
the exact recipe on hellofresh.co.uk, verifies it, checks Mealie for duplicates, and imports it with Mealie's own URL importer.
It reports back what was imported, already present, not found or failed.

- Python 3.13, [FastMCP](https://gofastmcp.com) 4.1, **Streamable HTTP** at `/mcp`
- **OAuth 2.1** authorisation server built in (discovery, dynamic client registration, PKCE, RFC 9207 `iss`), which is what
  Claude and ChatGPT custom connectors use. Approving a connection requires your owner password.
- Read and import only: **no tool can delete or overwrite a recipe**
- Docker image `crashandburnds92/mealie-mcp` (linux/amd64 + linux/arm64), non-root, read-only filesystem, health checks

---

## Contents

1. [Tools](#tools)
2. [How HelloFresh matching works](#how-hellofresh-matching-works)
3. [Security model](#security-model)
4. [Configuration](#configuration)
5. [Deploy on TrueNAS SCALE](#deploy-on-truenas-scale-web-ui-no-ssh)
6. [Cloudflare + Nginx Proxy Manager](#cloudflare--nginx-proxy-manager)
7. [Connect Claude](#connect-claude) · [Connect ChatGPT](#connect-chatgpt)
8. [Verify the deployment and first import](#verify-the-deployment-and-the-first-import)
9. [GitHub Actions → Docker Hub](#github-actions--docker-hub)
10. [Development](#development)
11. [Troubleshooting](#troubleshooting)

---

## Tools

| Tool | What it does | Writes? |
|---|---|---|
| `mealie_status` | Checks Mealie connectivity + token; returns Mealie version and user | no |
| `mealie_list_recipes` | Lists recipes (paged, newest first) | no |
| `mealie_search_recipes` | Searches the library | no |
| `mealie_get_recipe` | Full recipe: ingredients, steps, times, servings, nutrition | no |
| `mealie_check_duplicate` | Is this URL / exact name already in Mealie? | no |
| `mealie_import_url` | Import one recipe URL via Mealie's scraper (`dry_run` supported) | adds |
| `mealie_bulk_import` | Import many URLs; per-URL results + summary | adds |
| `hellofresh_search` | Search the HelloFresh UK catalogue by name (unverified hits) | no |
| `hellofresh_match` | Find **and verify** the exact recipe for a title (+ subtitle); returns ingredients, quantities, steps, times, servings, image, nutrition | no |
| `hellofresh_import` | Match → verify → duplicate-check → import (`dry_run` supported) | adds |
| `hellofresh_bulk_import` | The same for up to 20 recipe cards in one call | adds |
| `mealie_create_recipe` | Create a recipe from structured data, e.g. transcribed from a cookbook page photo (`dry_run` supported) | adds |

All "adds" tools skip recipes that already exist (by source URL, HelloFresh recipe id, or exact name) and never modify
existing ones. Set `DRY_RUN=true` to force every import into preview mode.

## How HelloFresh matching works

HelloFresh has no public API, so the server **never trusts a single search hit**:

1. **Search** with two independent strategies: the JSON search gateway that hellofresh.co.uk's own pages use (with the
   anonymous token embedded in the public site), then a fallback that parses recipe links from `/recipes/search`.
2. **Verify**: each promising candidate's recipe page is fetched and its schema.org `Recipe` data parsed (name, subtitle,
   ingredients, steps, nutrition, image).
3. **Match strictly**: a candidate is a match only when the normalised title is **identical** and, if you give a subtitle
   (the "with …" line on the card), it matches too (≥ 85 % token similarity). Similar recipes such as
   *Curried Chicken **Breast** Traybake* are reported as `alternatives` and are **never** imported automatically.
4. If HelloFresh search is unavailable or nothing matches, the tool says so. The assistant can then find the URL itself (web
   search) and pass it as `url` / `candidate_urls`, and that URL is verified the same way. URLs are never invented.

The first recipe, **Curried Chicken Thigh Traybake** *with Cumin Roast Potatoes, Carrots and Green Beans*, is
<https://www.hellofresh.co.uk/recipes/curried-chicken-thigh-traybake-64ad73db48c7b9302c333117>. (The *Breast* variant,
`…-64ef6810c09936aa866c1a15`, is a different recipe; the tests check it is never chosen.)

## Security model

| Concern | Mitigation |
|---|---|
| Internet exposure | HTTPS terminated at Nginx Proxy Manager (Cloudflare in front). The container only serves `/mcp`, OAuth endpoints, `/health`. |
| Authentication | OAuth 2.1 + PKCE (S256). Connecting a client requires the **owner password** on a consent page. Brute force: 5 failures per IP / 25 globally per 15 min. |
| Client registration | Open dynamic registration (Claude and ChatGPT need it), but redirect URIs must be `https://` on an allowlisted host (`claude.ai`, `chatgpt.com`, …) or a loopback address on your own computer (Claude Code CLI). |
| Tokens | 1 h access tokens + 30 day single-use rotating refresh tokens, HS256-signed, bound to `https://<host>/mcp` (audience). Rotate `AUTH_SECRET` to revoke everything. |
| Mealie token | Only in env vars / Docker secrets; redacted from logs; never returned by tools. |
| SSRF / LAN access | Import URLs must be `https`, on `IMPORT_ALLOWED_DOMAINS`, port 443, no IP literals, no `.local`/`.lan` names, and must resolve only to **public** IPs (Mealie fetches them). |
| Outbound calls | Each HTTP client is pinned to its host (your Mealie host; hellofresh.co.uk), including redirects. |
| Abuse / load | MCP request rate limit (5/s, burst 30), HelloFresh throttle (1 req/s), timeouts everywhere, retries with backoff on 429/5xx. |
| Container | Non-root (uid 10001), read-only root FS, all capabilities dropped, `no-new-privileges`, 256 MB memory limit. |
| Destructive operations | None exist: there are no delete, update or overwrite tools. |

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)). They can also be provided as Docker secrets
in `/run/secrets/<lowercase_name>`, for example `/run/secrets/mealie_api_token`.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MEALIE_URL` | ✅ | | `https://mealie.crashtech.co.uk` (or LAN URL, see below) |
| `MEALIE_API_TOKEN` | ✅ | | Mealie → *Profile → Manage API Tokens* |
| `PUBLIC_BASE_URL` | ✅ | `http://localhost:8000` | `https://mcp.crashtech.co.uk`: must be the exact public origin |
| `OWNER_PASSWORD` | ✅ (oauth) | | ≥ 12 chars; typed when you connect Claude / ChatGPT |
| `AUTH_SECRET` | ✅ (oauth) | | ≥ 32 chars; `openssl rand -hex 32` |
| `AUTH_MODE` | | `oauth` | `none` only for local testing, **never** on the internet |
| `STATIC_TOKENS` | | | Optional comma-separated bearer tokens (≥ 32 chars) for scripts / other clients |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | | `chatgpt.com,chat.openai.com,claude.ai,claude.com` | |
| `OAUTH_ALLOW_LOOPBACK_REDIRECTS` | | `true` | Allow `http://localhost:<port>` sign-in callbacks (Claude Code CLI, MCP Inspector) |
| `IMPORT_ALLOWED_DOMAINS` | | `hellofresh.co.uk,pinchofnom.com` | Add more domains, or `*` for any public site |
| `DRY_RUN` | | `false` | Force all imports into preview mode |
| `BULK_IMPORT_MAX` | | `50` | |
| `MEALIE_IMPORT_INCLUDE_TAGS` | | `true` | Keep tags scraped from the recipe page |
| `MEALIE_TIMEOUT_SECONDS` / `MEALIE_IMPORT_TIMEOUT_SECONDS` | | `30` / `120` | |
| `MEALIE_VERIFY_TLS` | | `true` | |
| `HELLOFRESH_TIMEOUT_SECONDS` / `HELLOFRESH_MIN_INTERVAL_SECONDS` | | `20` / `1` | |
| `RATE_LIMIT_PER_SECOND` / `RATE_LIMIT_BURST` | | `5` / `30` | |
| `PORT` | | `8000` | Container port |
| `MCP_PATH` | | `/mcp` | |
| `LOG_LEVEL` / `LOG_FORMAT` | | `INFO` / `json` | `text` for human-readable |
| `DATA_DIR` | | `/data` | Stores only OAuth client registrations |

**Which `MEALIE_URL`?** The public `https://mealie.crashtech.co.uk` works if the container can reach it. If Cloudflare
security features (Bot Fight Mode, WAF, Access) block server-to-server API calls, use Mealie's LAN address instead, for
example `http://192.168.1.50:9925` (your TrueNAS IP and Mealie's port). Outbound calls are pinned to whichever host you
configure.

---

## Deploy on TrueNAS SCALE (web UI, no SSH)

Applies to TrueNAS SCALE 24.10 "Electric Eel" and later, where apps run on Docker. Mealie is not modified: this is a
separate app that talks to Mealie's REST API with your token.

### 1. Prepare secrets

On any computer, generate two random values (or use a password manager):

```bash
openssl rand -hex 32     # → AUTH_SECRET
openssl rand -base64 24  # → OWNER_PASSWORD (or choose a long passphrase)
```

### 2. Install as a Custom App

1. TrueNAS web UI → **Apps** → **Discover Apps**.
2. Click the **⋮** menu (top right) → **Install via YAML**.
3. Name: `mealie-mcp`.
4. Paste the YAML below, filling in the four values marked `CHANGE_ME`:

```yaml
services:
  mealie-mcp:
    image: crashandburnds92/mealie-mcp:latest
    restart: unless-stopped
    environment:
      MEALIE_URL: https://mealie.crashtech.co.uk
      MEALIE_API_TOKEN: CHANGE_ME_mealie_api_token
      PUBLIC_BASE_URL: https://mcp.crashtech.co.uk
      AUTH_MODE: oauth
      OWNER_PASSWORD: CHANGE_ME_long_password
      AUTH_SECRET: CHANGE_ME_64_hex_chars_from_openssl
      IMPORT_ALLOWED_DOMAINS: hellofresh.co.uk,pinchofnom.com
      DRY_RUN: "false"
      PORT: "8000"
      LOG_LEVEL: INFO
      LOG_FORMAT: json
    ports:
      - "8765:8000"
    volumes:
      - mealie-mcp-data:/data
    read_only: true
    tmpfs:
      - /tmp:size=16m
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
volumes:
  mealie-mcp-data:
```

5. **Save**. The app should show **Running** and, after ~15 s, healthy.
6. Check it from a browser on your LAN: `http://<truenas-ip>:8765/health` → `{"status":"ok",...}` and
   `http://<truenas-ip>:8765/health/ready` → `{"status":"ok","mealie":"ok"}` (this confirms the Mealie token works).
7. Logs: **Apps → mealie-mcp → Workloads → Logs** (icon on the container row). If configuration is missing, the log
   names the variable (never its value) and the container exits.

To **update**: Apps → mealie-mcp → **Edit** → Save (or *Update* when offered) to pull the latest `:latest` image. Pin a
version tag such as `crashandburnds92/mealie-mcp:1.0.0` if you prefer controlled upgrades.

> Port `8765` is only for Nginx Proxy Manager on your LAN. **Do not** port-forward it on your router; only NPM's 443
> should be reachable from the internet.

---

## Cloudflare + Nginx Proxy Manager

### Cloudflare DNS

1. Cloudflare dashboard → *crashtech.co.uk* → **DNS → Records → Add record**.
2. Copy whatever record `mealie` uses: e.g. **CNAME** `mcp` → same target as `mealie` (or **A** `mcp` → your public IP).
   Proxy status: **Proxied** (orange cloud) is fine.
3. **SSL/TLS → Overview**: *Full (strict)* (requires a valid certificate on NPM, which the step below provides).
4. If you use Bot Fight Mode / WAF custom rules / Cloudflare Access, make sure they don't challenge
   `mcp.crashtech.co.uk`. Claude's and ChatGPT's servers can't solve challenges. Add a WAF *Skip* rule for that hostname if needed.
5. Don't enable caching rules for this hostname (the defaults don't cache these responses).

### Nginx Proxy Manager proxy host

**Hosts → Proxy Hosts → Add Proxy Host**

*Details tab*

| Field | Value |
|---|---|
| Domain Names | `mcp.crashtech.co.uk` |
| Scheme | `http` |
| Forward Hostname / IP | your TrueNAS LAN IP (e.g. `192.168.1.50`) |
| Forward Port | `8765` |
| Cache Assets | **off** |
| Block Common Exploits | on |
| Websockets Support | **on** (sets HTTP/1.1 upstream; harmless for Streamable HTTP) |
| Access List | Publicly Accessible (Claude/ChatGPT must reach it; the app does its own OAuth) |

*SSL tab*: Request a new Let's Encrypt certificate (use the **DNS challenge → Cloudflare** with an API token if port 80
isn't open), enable **Force SSL**, **HTTP/2 Support** and **HSTS**.

*Advanced tab*: paste:

```nginx
# Streamable HTTP: stream responses (SSE) straight through, no buffering.
proxy_buffering off;
proxy_request_buffering off;
proxy_cache off;
gzip off;

# Long-running imports.
proxy_connect_timeout 30s;
proxy_send_timeout 300s;
proxy_read_timeout 300s;

client_max_body_size 1m;
```

NPM already forwards the client IP (`X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`), which the login rate
limiter uses, so no header lines are needed here.

This proxy host only forwards to the mealie-mcp container's port. The TrueNAS admin UI (ports 80/443 on the TrueNAS
host) is never proxied: don't create proxy hosts for it, and don't forward router ports to it.

Test from outside your network (e.g. phone on mobile data):

```bash
curl https://mcp.crashtech.co.uk/health
# {"status":"ok","version":"1.0.0"}
curl -i https://mcp.crashtech.co.uk/mcp -X POST -H 'Content-Type: application/json' -d '{}'
# HTTP/2 401 with a "WWW-Authenticate: Bearer ... resource_metadata=..." header: auth is enforced
curl https://mcp.crashtech.co.uk/.well-known/oauth-protected-resource/mcp
```

---

## Connect Claude

The server works with every Claude surface. Add it once on claude.ai and it is available in **claude.ai, Claude Desktop,
Claude mobile and Claude Code on the web**. Claude Code on your own computer can use it too.

### claude.ai / Desktop / mobile (and Claude Code on the web)

1. <https://claude.ai> → **Settings → Connectors** → **Add custom connector**.
2. **Name** `Mealie`, **Remote MCP server URL** `https://mcp.crashtech.co.uk/mcp`. If asked how to register, choose
   **Register automatically** (dynamic client registration). Leave client ID/secret empty.
3. **Add** → **Connect**. A window opens on `https://mcp.crashtech.co.uk/oauth/login`: enter your **OWNER_PASSWORD** →
   **Approve**. Claude's callback is `https://claude.ai/api/mcp/auth_callback`, which is on the default allowlist.
4. In a chat, enable **Mealie** from the tools/connectors menu (the "+" or slider icon), then ask, e.g.
   *"Add this HelloFresh recipe to my Mealie"* with a photo of the card.

Connectors added this way are also passed into **Claude Code on the web** sessions. Those sessions run in a sandbox
whose network policy must allow `mcp.crashtech.co.uk`, so add that host to the environment's allowed domains if the
connector shows as unreachable. Re-authorise at <https://claude.ai/customize/connectors> if a session reports the
connector needs sign-in.

### Claude Code (CLI on your computer)

Using OAuth: Claude Code opens your browser and receives the sign-in on a `http://localhost:<port>/callback` loopback
address, which is allowed by `OAUTH_ALLOW_LOOPBACK_REDIRECTS=true`:

```bash
claude mcp add --transport http --scope user mealie https://mcp.crashtech.co.uk/mcp
claude mcp login mealie      # or run /mcp inside Claude Code; enter OWNER_PASSWORD in the browser
```

Or using a static token (no browser; set a long random value in `STATIC_TOKENS` on the server first):

```bash
claude mcp add --transport http --scope user mealie https://mcp.crashtech.co.uk/mcp \
  --header "Authorization: Bearer YOUR_STATIC_TOKEN"
```

Or per project, committed as `.mcp.json` with the token read from an environment variable (never commit the token):

```json
{
  "mcpServers": {
    "mealie": {
      "type": "http",
      "url": "https://mcp.crashtech.co.uk/mcp",
      "headers": { "Authorization": "Bearer ${MEALIE_MCP_TOKEN}" }
    }
  }
}
```

---

## Connect ChatGPT

Custom MCP connectors are added in ChatGPT's **Developer mode** (a paid-plan feature; OpenAI changes plan availability
and menu names from time to time, so if the steps below differ search [help.openai.com](https://help.openai.com) for
"Developer mode"). Deploying the server does not by itself make it visible to ChatGPT; you must add it as below.

1. ChatGPT (web) → **Settings → Apps & Connectors → Advanced settings** → enable **Developer mode**.
2. **Settings → Apps & Connectors → Create** (or *Add app / Add MCP server*).
3. Fill in:
   - **Name**: `Mealie`
   - **Description**: `My Mealie recipe library and HelloFresh UK importer`
   - **MCP Server URL**: `https://mcp.crashtech.co.uk/mcp`
   - **Authentication**: **OAuth** (leave client ID/secret empty; the server supports dynamic registration)
   - Tick *"I trust this application"*.
4. Click **Create**. ChatGPT opens a window on `https://mcp.crashtech.co.uk/oauth/login`: enter your
   **OWNER_PASSWORD** and click **Approve**. You are returned to ChatGPT and the 11 tools are listed.
5. In a chat, enable the connector: **+ → More → Developer mode → Mealie**. ChatGPT asks for confirmation before
   tools that write (`hellofresh_import`, …); read-only tools are marked as such.

Try:

> Check my Mealie connection.
>
> Add this HelloFresh recipe to Mealie: *Curried Chicken Thigh Traybake, with Cumin Roast Potatoes, Carrots and Green Beans*. Do a dry run first.
>
> *(attach photos)* Add these HelloFresh recipes to my Mealie. Read the title and the "with…" subtitle from each card.

To disconnect: delete the connector in ChatGPT. To revoke every issued token server-side, change `AUTH_SECRET` and
restart the app.

Other clients: anything that supports remote MCP + OAuth works the same way. Scripts can use a `STATIC_TOKENS` value
as `Authorization: Bearer …`.

---

## Verify the deployment and the first import

After connecting Claude or ChatGPT:

1. Ask *"Run mealie_status"* → expect `authenticated: true` and your Mealie version.
2. Ask *"Use hellofresh_import with dry_run=true for 'Curried Chicken Thigh Traybake', subtitle 'with Cumin Roast
   Potatoes, Carrots and Green Beans'"*. Expect `status: matched`, URL
   `…/curried-chicken-thigh-traybake-64ad73db48c7b9302c333117`, `import_status: dry_run`.
3. Repeat without dry run → `import_status: imported` and a link to the new recipe in Mealie.
4. Run it again → `import_status: duplicate` (nothing changed in Mealie).

Without an AI app, using a static token (set `STATIC_TOKENS` first):

```bash
TOKEN=your-static-token
curl -s https://mcp.crashtech.co.uk/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hellofresh_import","arguments":{"name":"Curried Chicken Thigh Traybake","subtitle":"with Cumin Roast Potatoes, Carrots and Green Beans","dry_run":true}}}'
```

---

## GitHub Actions → Docker Hub

`.github/workflows/docker.yml` runs on every push and pull request:

1. **test**: installs pinned deps, runs `ruff` and `pytest`.
2. **docker**: builds the image, starts it and smoke-tests `/health`, the 401 on `/mcp`, and OAuth discovery.
3. **publish** (pushes to `main` and `v*` tags only): multi-arch build `linux/amd64,linux/arm64`, pushes
   `crashandburnds92/mealie-mcp` with tags `latest` (main), `sha-<commit>`, and `1.2.3` / `1.2` for `v1.2.3` git tags.

A failure in any job fails the run (red ✗ on the commit / PR, and GitHub emails you). The job summary names the
failing step.

### One-time setup: Docker Hub secrets

1. Create a Docker Hub access token: <https://app.docker.com/settings/personal-access-tokens> → **Generate new token**,
   description `github-actions-mealie-mcp`, permissions **Read & Write**. Copy it.
2. Add repository secrets: <https://github.com/Crashandburnds/mealie-mcp/settings/secrets/actions> →
   **New repository secret**:
   - `DOCKERHUB_USERNAME` = `crashandburnds92`
   - `DOCKERHUB_TOKEN` = the token from step 1
3. Re-run the latest workflow: <https://github.com/Crashandburnds/mealie-mcp/actions> → latest run → **Re-run all jobs**
   (or push any commit to `main`).

To cut a release: `git tag v1.0.0 && git push origin v1.0.0`.

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -e .
ruff check src tests && pytest -q

# run locally without auth against your Mealie (LAN only!)
MEALIE_URL=https://mealie.crashtech.co.uk MEALIE_API_TOKEN=... AUTH_MODE=none LOG_FORMAT=text python -m mealie_mcp
npx @modelcontextprotocol/inspector   # connect to http://localhost:8000/mcp
```

Tests (72) cover startup, health, Mealie auth/list/search/get, imports, duplicates, bulk import, dry runs, HelloFresh
search/fallback/verification/strict matching (Thigh vs Breast), invalid URLs and SSRF, API failures/timeouts/rate limits,
the full OAuth flow (registration, PKCE, consent, token, refresh rotation, lockout, CSRF, forged tokens), unauthorised
access, and log redaction. External services are mocked; no real Mealie or HelloFresh calls are made in CI.

Layout: `src/mealie_mcp/` — `server.py` (tools, app), `mealie.py` (Mealie client), `hellofresh.py` (search/verify/match),
`auth.py` (OAuth provider), `net.py` (URL validation, SSRF guard, pinned HTTP clients), `config.py`, `logging_setup.py`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Container exits, log `configuration error: OWNER_PASSWORD ...` | Set the named variable (min lengths: password 12, secret 32). |
| `/health/ready` → `mealie: unavailable` | Wrong `MEALIE_URL`/token, or Cloudflare blocking the server → use Mealie's LAN URL. |
| Claude/ChatGPT: "error creating connector" / OAuth fails | `PUBLIC_BASE_URL` must be exactly `https://mcp.crashtech.co.uk`; check `curl https://mcp.crashtech.co.uk/.well-known/oauth-authorization-server`. Check Cloudflare isn't challenging the host. |
| Login page says "link expired" | Sign-in links last 10 minutes; start again from your AI app. |
| "Too many failed attempts" | Wait 15 minutes (or restart the app). |
| `hellofresh_*` → `search_unavailable` | HelloFresh changed their site or blocked the request. Ask the assistant to find the hellofresh.co.uk URL by web search and pass it as `url`; it is still verified. |
| Import → `failed: Recipe scraping failed` | Mealie's scraper couldn't parse the page; try again later or import manually in Mealie. |
| The assistant times out on big batches | Ask for batches of ~5 recipes per call. |
| 524 errors from Cloudflare | Make sure NPM has `proxy_buffering off` (Advanced tab). |
