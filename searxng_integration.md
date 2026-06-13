# SearXNG — Installation and Configuration Guide

This document covers installing SearXNG via Docker and configuring it for use with aidir.

---

## 1. Docker installation

```bash
mkdir -p ./searxng
cd ./searxng

# Download the official docker-compose and env template
curl -fsSL \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example

cp .env.example .env
```

Edit `.env` — set instance name, secret key, and port:

```bash
# .env
INSTANCE_NAME=my-searxng
SECRET_KEY=<generate with: openssl rand -hex 32>
BIND_ADDRESS=127.0.0.1:18080   # use 0.0.0.0:18080 to expose on all interfaces
```

Start the service:

```bash
docker compose up -d
```

Check it is running:

```bash
curl -s http://127.0.0.1:18080/healthz
# expected: 200 OK
```

---

## 2. SearXNG configuration (`settings.yml`)

SearXNG reads its configuration from `searxng/settings.yml` (mounted by Docker).
The file is generated on first run if it does not exist.

To obtain the generated file and edit it:

```bash
# Copy the generated file out of the container (only needed once)
docker cp searxng:/etc/searxng/settings.yml ./settings.yml
```

Then edit `./settings.yml` and mount it back into the container via `docker-compose.yml`:

```yaml
# in docker-compose.yml, under the searxng service volumes:
volumes:
  - ./settings.yml:/etc/searxng/settings.yml:ro
```

### 2.1 Enable JSON format (required for aidir integration)

SearXNG must allow the `json` output format so aidir can query it programmatically.

```yaml
# settings.yml
search:
  formats:
    - html       # keep web UI working
    - json       # required for aidir
```

### 2.2 Remove rate limiting for local callers (optional)

If aidir runs on the same host, you can disable the built-in bot protection:

```yaml
server:
  limiter: false   # disable rate limiting for local use
  secret_key: "<same as SECRET_KEY in .env>"
```

### 2.3 Select active search engines

Control which upstream engines SearXNG uses. Disable anything you do not want:

```yaml
engines:
  - name: google
    engine: google
    shortcut: g
    disabled: false

  - name: bing
    engine: bing
    shortcut: b
    disabled: false

  - name: duckduckgo
    engine: duckduckgo
    shortcut: d
    disabled: false

  # Disable engines that require additional setup or are too slow:
  - name: wikidata
    engine: wikidata
    shortcut: wd
    disabled: true
```

### 2.4 Language and safe search defaults

```yaml
search:
  default_lang: "all"
  safe_search: 0   # 0=off, 1=moderate, 2=strict
```

After editing, restart the container to apply:

```bash
docker compose restart searxng
```

---

## 3. Verify JSON search works

```bash
curl -s "http://127.0.0.1:18080/search?q=test&format=json&language=all&categories=general" | python3 -m json.tool | head -40
```

Expected response contains a `results` array with `url`, `title`, `content` fields.

---

## 4. aidir integration

Add SearXNG to the `providers` list in `config.json5` (see `proposal_searxng.md` for the full design).

Short example config:

```json5
"web_search": {
  "request_timeout": 30,
  "provider_cooldown_seconds": 60,
  "providers": [
    {
      "id": "brave",
      "type": "brave",
      "enabled": true,
      "apiKey": "${BRAVE_APIKEY}"
    },
    {
      "id": "local_searxng",
      "type": "searxng",
      "enabled": true,
      "host": "${SEARXNG_HOST:-127.0.0.1}",
      "port": ${SEARXNG_PORT:-18080},
      "engines": [],              // empty = use SearXNG defaults
      "categories": ["general"],
      "language": "all",
      "safesearch": 0
    }
  ]
}
```

Add these to your `.env`:

```bash
SEARXNG_HOST=127.0.0.1
SEARXNG_PORT=18080
```

After updating the config and `.env`, restart aidir.
To confirm SearXNG is being used, check `logs/workers.log` — it will log which provider handled each request and any fallback events.

---

## 5. Run SearXNG only (no Brave)

Remove the `brave` entry from `providers` (or set `"enabled": false`) and leave only the `searxng` entry.
aidir will query SearXNG for every search and fetch request with no Brave fallback.