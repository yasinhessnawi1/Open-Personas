# Azure voice host: requirements for the infrastructure (Terraform) session

Status: DRAFT, 2026-11-07, written from the code at `32d7d175` (branch `feat/voice-arm`).
Audience: someone provisioning the Azure side who has never seen this codebase.
This file states requirements only. It contains no Terraform and no secret values.

Every claim below cites the file and line it comes from (paths are relative to the repo
root). Where the code does not answer a question, the text says so and names it as an
open item or a parameter.

## 0. Decided inputs and remaining parameters

Decided (from the Oracle deployment plan, 2026-11-07):

| Input | Value | Where it matters |
|---|---|---|
| `ORACLE_REGION` | `eu-frankfurt-1` (OCI Frankfurt): the api and Postgres | section 10 |
| `AZURE_REGION` | `germanywestcentral` (Azure Frankfurt), chosen by the rule in section 10 | section 10 |
| `DB_LINK` | a WireGuard tunnel from this VM to the Oracle host; on the Oracle side a small TCP proxy (`pglink`) forwards to Postgres and listens **only on the tunnel address**; voice logs in as a dedicated `persona_voice` role | section 4.4 |

Parameters still open:

| Parameter | Meaning | Who decides |
|---|---|---|
| `VOICE_HOST` | Public name of the voice HTTP service. Production used `voice.openpersonasai.com` (`deploy/hetzner/Caddyfile:83`). | owner |
| `LIVEKIT_HOST` | Public name of LiveKit signaling. Production used `livekit.openpersonasai.com` (`deploy/hetzner/Caddyfile:102`). | owner |
| `WEB_ORIGINS` | Origin(s) of the web app, for CORS (`packages/voice/src/persona_voice/config.py:143`). | owner |
| `ADMIN_CIDRS` | Addresses allowed to reach SSH. | owner |
| `N_CALLS` | Design point for concurrent calls. This brief sizes for 5 and requires a measurement (section 2.4). | owner |

## 1. What runs on this VM, and what talks to what

The owner ruled on 2026-11-07: production voice (the voice service, `packages/voice`, and
the LiveKit media server, `packages/livekit`) runs on one Azure x86_64 VM; everything else
(the api, its in-process worker and connectors, Postgres) runs on an Oracle ARM server. The
web app is on Vercel.

Three containers on the Azure VM, the same shape as the single-box kit in
`deploy/hetzner/docker-compose.yml`, minus postgres and the api:

| Container | Image | Role |
|---|---|---|
| `voice` | built from `packages/voice/Dockerfile` | FastAPI on port 8001 (token issue, voice catalogue, one-shot TTS and STT) plus the in-process LiveKit agent that becomes the persona on each call (`packages/voice/Dockerfile:3-13`, `packages/voice/src/persona_voice/http/app.py:596-631`). One process, one uvicorn worker, never more than one replica (`packages/voice/Dockerfile:12-13,88-92`, `deploy/hetzner/docker-compose.yml:178-181`). |
| `livekit` | `livekit/livekit-server:v1.13.1` plus the repo's config and entrypoint (`packages/livekit/Dockerfile:14-26`) | WebRTC SFU, audio only, single node, no Redis (`packages/livekit/livekit.yaml:17-18`). |
| `caddy` | `caddy:2.10.2` | TLS edge for both public names, Let's Encrypt via HTTP-01 (`deploy/hetzner/Caddyfile:1-32`, `deploy/hetzner/docker-compose.yml:260-275`). |

Flows (all verified in code):

```
 browser (Norway) --HTTPS--> VOICE_HOST:443 --> caddy --> voice:8001     POST /v1/voice/token, GET /v1/voices
 browser          --WSS----> LIVEKIT_HOST:443 --> caddy --> livekit:7880 signaling
 browser          --UDP 7882 / TCP 7881--> livekit (direct, not through caddy)   media
 voice agent      --WSS + UDP--> LIVEKIT_HOST (its own public address)  joins each call as the persona
 api (Oracle)     --HTTPS--> VOICE_HOST:443   GET /v1/voices, POST /v1/tts, POST /v1/stt (user's bearer forwarded)
 voice            --WireGuard (UDP) --> Oracle host --> pglink --> Postgres   every read and write voice makes
 voice            --HTTPS/WSS--> Deepgram, Cartesia, model providers, Hugging Face
```

Facts behind the arrows:

* **The api never contacts LiveKit.** No LiveKit client, URL or key is read anywhere in
  `packages/api/src` (searched; the only mentions are billing labels). LiveKit tokens are
  signed locally by voice with the API key and secret (`packages/voice/src/persona_voice/http/app.py:679-697`), and LiveKit has no webhook configured (`packages/livekit/livekit.yaml`, whole file).
* **Voice never calls the api over HTTP.** It reads and writes Postgres directly with the
  same RLS role the api uses (`packages/voice/src/persona_voice/config.py:112-115`,
  `deploy/hetzner/env/voice.env.example:8-9`). Delegated tasks, titles and synthesis are
  enqueued as rows (`packages/voice/src/persona_voice/session/delegation_enqueue.py:1`,
  `session/title_enqueue.py`, `session/synthesis_enqueue.py`), so there is no
  voice-to-api credential. The shared secrets are the database role and the Clerk public key.
* **The api calls voice** at `PERSONA_VOICE_SERVICE_URL` (`packages/api/src/persona_api/config.py:769`), forwarding the end user's own bearer token, no service credential (`packages/api/src/persona_api/routes/voice.py:145-155`). On the single box this was the internal `http://voice:8001` (`deploy/hetzner/docker-compose.yml:145`); with the api on Oracle it becomes `https://VOICE_HOST`.
* **The browser calls voice directly** at `NEXT_PUBLIC_VOICE_BASE_URL` (`packages/web/src/lib/voice/config.ts:13-14`) and learns the LiveKit URL from the token response (`packages/web/src/lib/voice/config.ts:9-11`).
* **The agent connects to the same public LiveKit URL the browser gets** (`packages/voice/src/persona_voice/agent/runner.py:1161,1180`, `transport/room.py:145-152`). On Azure the public IPv4 is not on the VM's interface (1:1 NAT), so the agent reaches its own LiveKit by hairpin through the public address. See 4.1 and acceptance item A9.

## 2. VM shape

### 2.1 What the code loads (memory)

Per voice process, at startup (`packages/voice/src/persona_voice/http/app.py:615-631`,
`agent/launcher.py:131-200`):

* PyTorch plus sentence-transformers (pulled by `persona-core`, `packages/core/pyproject.toml:46`).
* `BAAI/bge-small-en-v1.5` embedder on CPU, warmed and blocking at boot (`agent/launcher.py:44,136-142,176-193`).
* The crisis encoder `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, warmed in the background (`packages/runtime/src/persona_runtime/crisis_encoder.py:88`, `agent/launcher.py:147-151,194-202`).
* With graph memory and rerank enabled, the cross-encoder `cross-encoder/ms-marco-MiniLM-L6-v2` (`packages/voice/src/persona_voice/model/graph.py:232-248`, `packages/core/src/persona/recall/config.py:95`).
* Silero VAD (ONNX, about 2 MB, bundled in the wheel; no download).

Previous limits: Fly gave voice 2 GB (`packages/voice/fly.toml:91-96`); the single-box kit caps
it at 4 GB (`deploy/hetzner/docker-compose.yml:210`), LiveKit at 1 GB (`:256`), Caddy at 256 MB (`:273`).
Budget on this VM: 4 GB voice + 1 GB LiveKit + 0.25 GB Caddy + about 1 GB OS and Docker = about 6.3 GB.

### 2.2 What the code does per call (CPU)

* One process, one event loop, one uvicorn worker for every call (`packages/voice/Dockerfile:88-92`).
  The per-user cap is one call at a time (`packages/voice/src/persona_voice/concurrency.py:1-25`);
  **there is no global cap on concurrent calls in the code.**
* Per call: Opus decode and encode in LiveKit's native library, VAD every 32 ms on a worker
  thread (`stt/vad_silero.py:299-352`), streaming STT and TTS sockets, and per turn the
  embedder, the crisis encoder and (if on) the reranker on worker threads.
* LiveKit forwards two audio tracks per call (user and agent). Audio only SFU work is
  latency sensitive, not heavy (`packages/livekit/fly.toml:95-105`).
* **The one measurement that exists:** R9-216 (docs repo, `specs/phase3/spec_R9/issues.md`,
  entry R9-216). One call on Fly `shared-cpu-1x` 2 GB: first audio 5.9 s, 4.8 s, 4.0 s, then
  two turns with no audio at all, against a budget of 800 ms P50 and 1.5 s P95. Diagnosis:
  event loop starvation on one shared core. Requirement recorded there: **the voice host
  needs dedicated CPU, not a shared core**, and the move is not done until a real call meets
  the 800 ms P50 budget on the new host.

### 2.3 Recommendation

**Standard_D4as_v5 (4 vCPU, 16 GiB), x86_64, in `AZURE_REGION`.** This holds voice,
LiveKit and Caddy together.

Reasoning:

* Azure Dasv5 vCPUs are SMT threads: 4 vCPU is 2 physical cores. Two cores let the voice
  event loop, the model inference threads and LiveKit's packet relay run without all
  contending for one core, which is the R9-216 failure shape.
* 16 GiB leaves about 9 GiB of headroom over the 6.3 GB budget (2.1) for the page cache,
  image pulls and a second model warming during a deploy.
* Nothing needs a GPU or AVX-512. The image ships CUDA libraries that are never used (see 7.2),
  which costs disk, not CPU.

**Standard_D2as_v5 (2 vCPU, 8 GiB) is not recommended for this VM.** 2 vCPU on Dasv5 is one
physical core with two threads. Voice alone (no LiveKit) on it could carry one or two calls,
but the ruling puts LiveKit on the same server, and one core shared by the event loop, torch
inference and the SFU is exactly what R9-216 measured failing. 8 GiB also leaves under 2 GiB
of headroom. If cost forces it, it must pass the same R9-216 measured call (acceptance A11)
with the same load, and the owner should accept that it will not hold N_CALLS above 1 or 2.

Do not use: burstable B-series (CPU credits are the shared-core shape again); any Arm64
Azure size for this VM. Voice does run on Linux arm64 (community edition, tested in CI by
`.github/workflows/voice-platforms.yml`), but production voice is x86 by ruling, and the
sizing and the R9-216 measurement here are for x86. Worth checking but not verified here: a
compute optimized size without SMT (for example the Fasv6 family) in `AZURE_REGION`, which
would give 2 full cores in 2 vCPU; confirm availability and core topology before choosing it.

Enable **Accelerated Networking** on the NIC (lower jitter for UDP media). Dasv5 supports it.

### 2.4 Concurrency: what is known and what must be measured

The code has no load test for concurrent calls. An estimate, to be replaced by a
measurement: steady per-call work is a few percent of a core, with bursts per turn when the
embedder and encoders run. The ceiling is the single Python process (one event loop, the
GIL for everything that is not inside torch or native code), not the core count. Plan for
**N_CALLS = 5** concurrent calls on D4as_v5 within the 800 ms P50 budget, and treat 10 as the
point where one process is likely to saturate. Acceptance item A12 measures it. Scaling past
one voice process is not designed today (single worker by design, `packages/voice/Dockerfile:12-13`).

## 3. OS, Docker and host settings

The single-box kit's provisioning script is the reference (`deploy/hetzner/bin/provision.sh`);
it targets the same needs.

* **OS:** Ubuntu Server 24.04 LTS, x86_64 (`deploy/hetzner/bin/provision.sh:100-101`). Gen2 image.
* **Docker:** Docker Engine from Docker's apt repository with the compose v2 plugin and buildx
  (`deploy/hetzner/bin/provision.sh:160-171`). Compose features in use: profiles,
  `network_mode: host`, `mem_limit`, `pids_limit` (`deploy/hetzner/docker-compose.yml`).
* **`/etc/docker/daemon.json`:** json-file logs with rotation (50 MB x 5), `live-restore: true`,
  `no-new-privileges: true` (`deploy/hetzner/bin/provision.sh:174-180`).
* **Host firewall:** ufw default deny inbound, allow outbound, with the ports in 4.1 only
  (`deploy/hetzner/bin/provision.sh:192-199`). The Azure NSG must match it (both layers).
  Docker-published ports bypass ufw, so containers publish only on 127.0.0.1; caddy and
  livekit use the host network so ufw governs them (`deploy/hetzner/docker-compose.yml:7-13`).
* **Metadata endpoint:** bridged containers must not reach 169.254.169.254
  (`deploy/hetzner/systemd/open-persona-metadata-block.service`). On Azure this matters more
  than on Hetzner: if the VM has a managed identity, IMDS on that address mints its tokens.
* **Patching:** security updates install unattended, **the box never reboots by itself**
  because a reboot drops live calls (`deploy/hetzner/bin/provision.sh:144-147`). Set Azure
  guest patching to a mode that does not auto reboot, and plan reboots in a maintenance window.
* **Time:** keep the Azure default time sync (chrony). JWT and LiveKit token checks depend on it.
* **Directory for persistent data:** `/srv/open-persona/voice` owned by uid 1000, mounted as
  the voice container's `/var/lib/persona-voice` (`deploy/hetzner/docker-compose.yml:197-199`,
  `deploy/hetzner/bin/provision.sh:24`). Holds the Hugging Face model cache only (section 7).
* **SSH:** key only, no root login, rate limited (`deploy/hetzner/bin/provision.sh:10,194`),
  NSG source limited to `ADMIN_CIDRS` (or Azure Bastion / just in time access).

## 4. Network

### 4.1 Inbound (NSG and ufw both)

| Port | Proto | Source | Purpose | Evidence |
|---|---|---|---|---|
| 443 | TCP | Internet | Caddy: HTTPS for `VOICE_HOST`, WSS signaling for `LIVEKIT_HOST` | `deploy/hetzner/Caddyfile:74-108` |
| 80 | TCP | Internet | Caddy: ACME HTTP-01 challenge and redirect to HTTPS | `deploy/hetzner/Caddyfile:3-7`, `provision.sh:195` |
| 7881 | TCP | Internet | LiveKit ICE over TCP (media fallback for networks without UDP) | `packages/livekit/livekit.yaml:30-31`, `provision.sh:197` |
| 7882 | UDP | Internet | LiveKit ICE over UDP, single-port mux: **the primary media path** | `packages/livekit/livekit.yaml:32-35`, `provision.sh:198` |
| 22 | TCP | `ADMIN_CIDRS` only | SSH | `provision.sh:194` |
| 443 | UDP | Internet, optional | HTTP/3 to Caddy. Not required; clients fall back to TCP. | Caddy default behaviour |

Requirements on the public address:

* **A static, Standard SKU, instance-level public IPv4 attached to the VM's NIC.** No Azure Load
  Balancer and no NAT in front with port translation. LiveKit advertises this address and port
  7882 as its ICE candidate, so external port must equal internal port (`packages/livekit/livekit.yaml:32-35`), and the address is passed to the container as `LIVEKIT_NODE_IP`
  (`packages/livekit/entrypoint.sh:29-45`, `deploy/hetzner/env/livekit.env.example:8-10`).
  The address must therefore be known before LiveKit starts.
* **Hairpin:** the voice agent on this VM connects to its own public address (section 1).
  An Azure Load Balancer frontend does not hairpin, so use the instance-level public IP; that
  path is expected to work but is not proven for this stack. Acceptance item A9 proves it. If it
  fails, the fix belongs to the code side (a VM-local signaling URL for the agent plus a LiveKit
  candidate on the VM's private address), not a network workaround that changes the address
  advertised to browsers.
* **No TURN.** TURN is present but disabled in the config (`packages/livekit/livekit.yaml:48-60`); open no TURN ports (3478, 5349). Known limit: a network that allows only outbound TLS on 443 cannot carry media (`deploy/hetzner/RUNBOOK.md`, "Known limit, unchanged from Fly").
* IPv6 is not required. LiveKit advertises one IPv4 (`packages/livekit/entrypoint.sh:64-66`).
* Azure's default 4 minute TCP idle timeout on public IP flows is fine for LiveKit signaling
  (the client pings) but see 4.4 for the database connection.

### 4.2 What must NOT be exposed

| Port | Why it must stay closed |
|---|---|
| 8001/tcp (voice uvicorn) | Runs with `--proxy-headers --forwarded-allow-ips=*` (`packages/voice/Dockerfile:90-92`): it trusts forwarded headers from any client, which is safe only behind Caddy on loopback. Publish on 127.0.0.1 only (`deploy/hetzner/docker-compose.yml:200-201`). |
| 7880/tcp (LiveKit HTTP and signaling, plaintext) | Tokens travel in the query string; only Caddy on 127.0.0.1 may reach it (`deploy/hetzner/docker-compose.yml:10-13`, `deploy/hetzner/RUNBOOK.md` UDP check 2). |
| 2375, 2376 (Docker API) | No container mounts the Docker socket; the daemon is never on TCP (`deploy/hetzner/docker-compose.yml:18-20`). |
| 5432 | There is no Postgres on this VM. Do not open an inbound DB port here. |
| Any access log that records request URIs on `LIVEKIT_HOST` | LiveKit signaling carries its access token in the query string. Caddy drops the URI and the Authorization and Cookie headers from its log (`deploy/hetzner/Caddyfile:13-14,24-31`). If any Azure component is put in front (Application Gateway, Front Door) or flow or access logging is enabled, it must not log request URIs or these headers. Recommended: no Azure L7 component in front at all. |

### 4.3 Outbound

Keep the NSG default allow-outbound. The voice process composes a tool set (web search, web
fetch, diagrams, image generation: `packages/voice/src/persona_voice/model/tools.py:67-68`,
`agent/runner.py:767`) and model provider chains whose hosts change with configuration, so an
egress allowlist would break calls silently. For reference, the hosts the code names:

| Destination | Port | Why | Evidence |
|---|---|---|---|
| The Oracle host's WireGuard endpoint | its WireGuard UDP port | the tunnel that carries every voice read and write to `pglink` and Postgres (4.4) | `config.py:112-115` |
| `api.deepgram.com` (WSS) | 443 | streaming STT, default provider | `stt/config.py:147`, `stt/deepgram_backend.py:9` |
| Cartesia API (WSS) | 443 | streaming TTS, default provider | `tts/config.py:129` |
| `api.gladia.io`, `api.elevenlabs.io` | 443 | only if those providers are selected | `stt/gladia_backend.py:101-103`, `tts/elevenlabs_backend.py:91-92` |
| `openrouter.ai` and any provider in the model chains (`api.anthropic.com`, `api.deepseek.com`, `api.groq.com`, `integrate.api.nvidia.com`, `api.cloudflare.com`) | 443 | the persona's replies; the OpenRouter catalogue probe at boot | `packages/core/src/persona/backends/config.py:43-63`, `agent/openrouter_mode.py:1-20` |
| `huggingface.co` and the storage hosts it redirects to | 443 | first-boot download of the three models in 2.1 into the persistent cache | `deploy/hetzner/docker-compose.yml:195-199`, `packages/voice/fly.toml:82-89` |
| `LIVEKIT_HOST` (this VM's own public IP) | 443, 7881, 7882/UDP | the agent joins calls | section 1 |
| Let's Encrypt ACME | 443 | Caddy certificates | `deploy/hetzner/Caddyfile:20-23` |
| Docker Hub, ghcr.io, PyPI, github.com (patchelf), Ubuntu and Docker apt | 443 | image builds and OS updates | `packages/voice/Dockerfile:34,60`, `packages/voice/docker/patchelf.sh`, `provision.sh:160-171` |

Clerk: **no outbound call.** Voice verifies Clerk tokens against a static PEM public key
(`config.py:107-110`, `http/app.py:302-325`), not a JWKS URL.

### 4.4 The database link (the biggest risk in this split)

Voice is not a thin media relay: every call opens its own RLS engine on the shared Postgres
(`agent/runner.py:666`, `session/state_machine.py:125-155`), plus a call-record engine
(`agent/runner.py:1250`), and the process keeps a pool of 5 for ownership checks
(`http/app.py:422`). Recall, transcripts (`agent/runner.py:816`), credits and the call record
all go over it during the call. The project's own migration notes say Postgres is the
chattiest component and that splitting it from its callers "rebuilds today's latency problem
in network form" (docs repo, `ops/hetzner_migration.md`, section "The original reason,
unchanged", first paragraph). The split ruled on 2026-11-07 does exactly that for voice, so:

1. **Round trip time from this VM to Postgres is a first-class latency input.** Measure it
   (acceptance A6). It is paid several times per spoken turn, inside the 800 ms budget.
2. **The link is a WireGuard tunnel (decided).** This VM runs a WireGuard peer to the Oracle
   host. On the Oracle side a small TCP proxy, `pglink`, forwards to Postgres and is published
   **only on the Oracle host's tunnel address**; Postgres itself is never on a public address.
   Requirements on this side:
   * WireGuard on the host (kernel module, `wg-quick` or systemd-networkd), started at boot,
     before the voice container. Outbound UDP to the Oracle host's WireGuard port; this VM
     needs **no inbound** WireGuard port if it is the side that initiates
     (`PersistentKeepalive` set, see item 4).
   * The tunnel's private key is a secret of this VM (secret store, never in the repo). The
     peer's public key and endpoint are configuration.
   * The voice container reaches the tunnel address over the host's routing (the bridged
     `backend` network routes out through the host, `deploy/hetzner/docker-compose.yml:15-16`);
     nothing else on the VM needs it.
   * The DSN points at the tunnel address and `pglink`'s port. The tunnel already encrypts;
     TLS inside it is optional and depends on what `pglink` forwards to.
   * `pglink` must be a plain TCP forwarder (one client connection to one server connection).
     It must not be a transaction-mode pooler (item 3).
3. **No transaction-mode pooler** (for example PgBouncer in transaction mode) between voice and
   Postgres. The RLS identity is a session-level setting made on each connection checkout
   (`session/state_machine.py:123,141-153`) and the per-user call lock is an advisory lock
   (`session/state_machine.py:135-139`); both break under transaction pooling. Direct or
   session pooling only.
4. **Keepalives under 4 minutes** on the path (Azure's public IP idle timeout):
   `PersistentKeepalive` on the tunnel (25 s is the usual value), and optionally the libpq
   keepalive parameters in the DSN. The per-call engines do not pre-ping (`session/state_machine.py:141`).
5. **A dedicated login role for voice: `persona_voice`.** It is a member of `persona_app`
   (inheriting its grants, so every RLS policy that applies to `persona_app` applies to it),
   `NOBYPASSRLS`, not a superuser, not the table owner. Voice needs exactly what the api's
   request path has: the code was written against `persona_app` (`config.py:112-114`,
   `deploy/hetzner/env/voice.env.example:8-9`). The point of the separate role: the credential
   that lives on the Azure VM rotates on its own, without touching the api's `persona_app`
   password. Creating the role and its grants is the Oracle side's job; this VM only holds
   its password.
6. **Connection budget** on the Oracle Postgres: the api plus this VM's pool of 5 plus a few
   connections per live call. The single-box kit set `max_connections=100` (`deploy/hetzner/docker-compose.yml:65-66`).
   A per-role `CONNECTION LIMIT` on `persona_voice` is a good backstop.

## 5. DNS and TLS

* Two A records to the VM's static public IPv4: `VOICE_HOST` and `LIVEKIT_HOST`
  (`deploy/hetzner/RUNBOOK.md`, DNS table: `voice.openpersonasai.com` and `livekit.openpersonasai.com`).
  Keep `LIVEKIT_HOST` even if LiveKit Cloud is ever used as the fallback (`deploy/hetzner/Caddyfile:99-101`).
  Lower the TTL to 60 s a day before cutover (docs repo, `ops/hetzner_migration.md`, cutover order).
* **Caddy on the VM terminates TLS** for both names with Let's Encrypt HTTP-01, so port 80 must
  be open and DNS must already point at the VM. Start on the staging CA, switch to production
  once both names answer (`deploy/hetzner/env/caddy.env.example:4-9`). Secret names: `ACME_EMAIL`, `ACME_CA`.
* Request bodies: the single-box Caddyfile capped `VOICE_HOST` at 1 MB. That was safe only
  because the api reached voice internally. With the api on Oracle, the api's dictation proxy
  sends up to 10 MiB of audio to `POST /v1/stt` through this edge
  (`packages/voice/src/persona_voice/http/app.py:113,852`,
  `packages/api/src/persona_api/routes/voice.py:293`). **The `VOICE_HOST` body limit must be at
  least 12 MB on `/v1/stt`, and stay 1 MB on every other path.** Not site-wide: FastAPI reads a
  body before it checks auth and voice has no rate limit, so a large anonymous body to the token
  or TTS route stalls the one voice event loop (about 476 ms per 12 MB request). The code side
  does exactly this on `feat/voice-arm` (`deploy/hetzner/Caddyfile:83-97`, two `handle` blocks),
  with a kit test that keeps the `/v1/stt` cap above voice's own upload ceiling and every other
  path at 1 MB; whatever edge config runs on this VM must carry the same split.
* Timeouts: the api's proxy waits up to 60 s (`packages/api/src/persona_api/routes/voice.py:82`);
  LiveKit WebSockets live as long as a call (token TTL 1 h, `config.py:101`; billing ceiling 2 h,
  `config.py:67`). The edge must not impose shorter read or idle timeouts. Caddy's defaults do not.
* Clients: browsers use `wss://LIVEKIT_HOST` and `https://VOICE_HOST`; the api uses `https://VOICE_HOST`.

## 6. Settings and secrets (NAMES only)

Store values in a secret store (Azure Key Vault or equivalent) and render them to env files
outside the checkout, mode 0600, as the kit does (`deploy/hetzner/env/common.env.example:1-19`).
Never print them (`deploy/hetzner/docker-compose.yml:22-23`).

### 6.1 Voice container

Fixed values (not secrets), set in the compose `environment:` (`deploy/hetzner/docker-compose.yml:185-197`):
`PERSONA_LOG_LEVEL`, `PERSONA_EDITION` (= `cloud`), `PERSONA_VOICE_JWT_ALGORITHMS` (= `RS256`),
`PERSONA_VOICE_JWT_AUDIENCE` (= `persona-api`), `PERSONA_VOICE_AGENT_INPROCESS` (= `true`),
`PERSONA_VOICE_AUDIT_BACKEND` (= `postgres`; with `cloud` the process refuses to boot if audit
would land in the temp dir, `packages/voice/fly.toml:71-80`, `http/app.py:600-611`), `HF_HOME`.

Secrets and per-deployment settings:

| Name | Notes | Evidence |
|---|---|---|
| `PERSONA_VOICE_DATABASE_URL` | psycopg URL: role `persona_voice`, host = the Oracle tunnel address, port = `pglink`'s port, optional keepalive parameters (4.4) | `voice.env.example:8-9`, `config.py:115` |
| WireGuard private key of this VM (host secret, not a container env) | the tunnel (4.4) | |
| `PERSONA_VOICE_JWT_PUBLIC_KEY` | Clerk production PEM | `voice.env.example:11-13` |
| `PERSONA_VOICE_CORS_ORIGINS` | `WEB_ORIGINS` | `voice.env.example:14-15` |
| `PERSONA_VOICE_LIVEKIT_URL` | `wss://LIVEKIT_HOST` | `voice.env.example:17-25` |
| `PERSONA_VOICE_LIVEKIT_API_KEY`, `PERSONA_VOICE_LIVEKIT_API_SECRET` | LiveKit key pair | `voice.env.example:26-27` |
| `PERSONA_VOICE_GRAPH_MEMORY_ENABLED`, `PERSONA_VOICE_DELEGATION_ENABLED` | feature switches | `voice.env.example:30-31`, `config.py:164,175` |
| `PERSONA_STT_PROVIDER`, `PERSONA_STT_MODEL`, `PERSONA_STT_API_KEY`, `PERSONA_GLADIA_API_KEY` | speech to text | `common.env.example:48-52`, `stt/config.py:147-168` |
| `PERSONA_STT_SILERO_ACTIVATION_THRESHOLD`, `PERSONA_STT_SILERO_MIN_SPEECH_DURATION_MS` | VAD tuning, see A13 | `voice.env.example:32-33`, `stt/config.py:199-201` |
| `PERSONA_TTS_PROVIDER`, `PERSONA_TTS_MODEL`, `PERSONA_TTS_API_KEY`, `PERSONA_TTS_VOICE_DEFAULT`, `PERSONA_ELEVENLABS_API_KEY` | speech synthesis | `common.env.example:53-57`, `tts/config.py:129-168` |
| The seven shared settings: `PERSONA_FRONTIER_MODELS`, `PERSONA_MID_MODELS`, `PERSONA_SMALL_MODELS`, `PERSONA_FREE_FRONTIER_MODELS`, `PERSONA_FREE_MID_MODELS`, `PERSONA_FREE_SMALL_MODELS`, `PERSONA_OPENROUTER_SUBSCRIPTION_MODE` | model chains | `common.env.example:21-28` |
| `PERSONA_OPENROUTER_API_KEY`, `PERSONA_OPENROUTER_BASE_URL`, `PERSONA_ANTHROPIC_API_KEY`, `PERSONA_DEEPSEEK_API_KEY`, `PERSONA_GROQ_API_KEY`, `PERSONA_NVIDIA_API_KEY`, `PERSONA_CLOUDFLARE_ACCOUNT_ID`, `PERSONA_CLOUDFLARE_API_KEY`, `PERSONA_PROVIDER`, `PERSONA_MODEL`, `PERSONA_API_KEY` | provider credentials | `common.env.example:30-42` |
| `PERSONA_WEB_SEARCH_PROVIDER`, `PERSONA_WEB_SEARCH_API_KEY` | the voice tool set | `common.env.example:44-46` |
| `PERSONA_RECALL_UNIFIED_ENABLED`, `PERSONA_RECALL_RERANK_ENABLED`, `PERSONA_CREDIT_MARKUP` | recall and billing | `common.env.example:59-62` |
| `CREDITS_MAX_PER_DAY` | per-user daily cap, unprefixed | `config.py:47-55` |
| Optional tuning, defaults are fine: `PERSONA_VOICE_LIVEKIT_TOKEN_TTL_S`, `PERSONA_VOICE_MAX_BILLABLE_CALL_MINUTES`, `PERSONA_VOICE_TURN_FIRST_AUDIO_TIMEOUT_S`, `PERSONA_VOICE_GREET_WARMUP_TIMEOUT_S`, `PERSONA_VOICE_GREET_TIMEOUT_S`, `PERSONA_VOICE_ASYNC_ARTIFACT_CONCURRENCY`, `PERSONA_SAFETY_*` | | `config.py:67,101,183-193`, `model/async_lane.py:51`, `packages/runtime/src/persona_runtime/safety_intercept.py:92` |

The full list of names lives in `deploy/hetzner/env/common.env.example` and
`deploy/hetzner/env/voice.env.example`; those files win over this table if they differ.

### 6.2 LiveKit container

| Name | Notes | Evidence |
|---|---|---|
| `LIVEKIT_KEYS` | `"<api key>: <api secret>"` | `packages/livekit/entrypoint.sh:10-12,47-51` |
| `LIVEKIT_NODE_IP` | the VM's static public IPv4 | `packages/livekit/entrypoint.sh:29-45` |
| `LIVEKIT_CONFIG_PATH` | fixed, `/etc/livekit/livekit.yaml` | `deploy/hetzner/docker-compose.yml:254-255` |

### 6.3 What must match, and where

| On this VM | Must equal | On |
|---|---|---|
| `PERSONA_VOICE_LIVEKIT_API_KEY` and `_API_SECRET` | the two halves of `LIVEKIT_KEYS` | this VM (LiveKit) |
| `PERSONA_VOICE_DATABASE_URL` role and password | the `persona_voice` role's password on the Oracle Postgres (its own credential; it does NOT equal the api's `persona_app` password, and rotates on its own) | Oracle |
| this VM's WireGuard public key and tunnel address | the Oracle peer's configuration (and `pglink` listening on the Oracle tunnel address) | Oracle |
| `PERSONA_VOICE_JWT_PUBLIC_KEY`, `_JWT_AUDIENCE`, `_JWT_ALGORITHMS` | `PERSONA_API_JWT_PUBLIC_KEY`, `PERSONA_API_JWT_AUDIENCE`, `PERSONA_API_JWT_ALGORITHMS` | Oracle |
| the seven shared settings | the api's copies (R9-213; checked in CI by `scripts/check_model_chains.py`) | Oracle |
| `PERSONA_EDITION`, `CREDITS_MAX_PER_DAY`, `PERSONA_TTS_PROVIDER` | the api's values (the api mirrors the TTS provider, `packages/api/src/persona_api/config.py:778`) | Oracle |
| `https://VOICE_HOST` | the api's `PERSONA_VOICE_SERVICE_URL`, and Vercel's `NEXT_PUBLIC_VOICE_BASE_URL` | Oracle, Vercel |
| `PERSONA_VOICE_CORS_ORIGINS` | the web app's real origin(s) | Vercel |

The api needs **no** LiveKit setting. There is **no** voice-to-api service credential.

## 7. Data at rest, disk, logs, backups

### 7.1 What this VM stores

* **No audio.** Nothing in `packages/voice/src` writes audio to disk; the one `wave.open` writes
  to an in-memory buffer for the `/v1/tts` response (`http/app.py:505`). LiveKit runs no egress
  or recording (`packages/livekit/livekit.yaml`, whole file).
* **No transcripts on this VM.** Raw transcript text is never written to audit or VoiceLog
  (`stt/audit.py:4,70`); conversation transcripts go to Postgres on Oracle (`agent/runner.py:816`).
  With `PERSONA_VOICE_AUDIT_BACKEND=postgres` the audit records go to Postgres too.
* **No tool outputs:** the voice tool set is built without a file persister (`agent/runner.py:767`).
* **The Hugging Face model cache** (public models, about 1 GB) under `/srv/open-persona/voice/hf`.
* **Container logs** (rotated, 7.3) and the env files (secrets).

### 7.2 Disk

* The voice image is large: about 3.1 GB of compressed wheels for x86_64, of which about 2.2 GB is
  the NVIDIA CUDA libraries and triton that torch pulls on Linux (torch's `sys_platform ==
  'linux'` dependencies in `uv.lock`) and that never run on this CPU-only host. Figures are
  computed from the wheel sizes in `uv.lock`. Expect about 7 GB installed, more while building.
* **OS disk: 128 GB Premium SSD if images are built on the VM** (the kit builds on the box,
  `deploy/hetzner/docker-compose.yml:173-177`); 64 GB is enough if images are built elsewhere
  and pulled. Alert at 85 percent (section 9).

### 7.3 Logs

json-file, 50 MB x 5 per container (`deploy/hetzner/docker-compose.yml:39-43`). No access logs
anywhere on the path (4.2). If logs are shipped off the box (Azure Monitor), the same rule
applies: no request URIs for `LIVEKIT_HOST`, no Authorization or Cookie headers, never env dumps.

### 7.4 Backups

Nothing on this VM needs backing up: no database, no user data (7.1). The VM must be
re-creatable from code plus the secret store. Back up, or keep in the secret store, the env
files' values and the LiveKit key pair. The model cache re-downloads (A4 covers a cold boot).

## 8. Health, restart, maintenance

* **Restart policy:** `unless-stopped` on all three containers; Docker enabled at boot,
  `live-restore` on (`deploy/hetzner/docker-compose.yml:211,257,274`, `provision.sh:174-183`).
* **Voice health:** there is no unauthenticated route; any HTTP status from
  `http://127.0.0.1:8001/v1/voices` (normally 401) proves the listener
  (`deploy/hetzner/docker-compose.yml:212-218`, `packages/voice/fly.toml:53-57`).
  **Start period 300 s:** the boot blocks until the embedder is warm, minutes on a cold cache
  (`packages/voice/fly.toml:62-67`, `http/app.py:615-627`). External probe: `GET https://VOICE_HOST/v1/voices` returns 401.
* **LiveKit health:** HTTP on 7880 answers on `/` (`packages/livekit/fly.toml:53-55`). External
  probe through Caddy: `GET https://LIVEKIT_HOST/` returns 200.
* **Stop:** SIGINT with a 30 s grace so calls end cleanly (`deploy/hetzner/docker-compose.yml:208-209`).
* **Maintenance:** no automatic reboot (section 3). Azure planned maintenance and host updates can pause
  or reboot the VM; a deploy or reboot drops live calls, so schedule both.

## 9. Monitoring and alerts

Minimum, matching the kit's alert model (`deploy/hetzner/bin/alert.sh:1-14`):

* External uptime probes every minute: `https://VOICE_HOST/v1/voices` expecting 401,
  `https://LIVEKIT_HOST/` expecting 200. Alert after 3 failures.
* TLS certificate expiry under 14 days on both names (Caddy renews; this catches a broken renewal).
* VM CPU: alert on sustained high CPU (for example over 70 percent for 10 minutes). CPU is the
  resource R9-216 showed failing first.
* VM memory over 85 percent; voice container restarts (OOM kills against the 4 GB limit).
* Disk over 85 percent (`deploy/hetzner/bin/diskcheck.sh:1-4`).
* Not monitorable by a simple probe: UDP media. The acceptance call (A8) is the check; repeat it
  after any network change.

## 10. Region and latency

Users are in Norway; the api and Postgres are in `eu-frankfurt-1` (OCI Frankfurt). Per spoken turn the
latency chain is: user to LiveKit (media), LiveKit to the agent (same VM), agent to the STT
provider, several sequential agent to Postgres round trips (4.4), agent to the model provider,
agent to the TTS provider, back to the user. The budget is 800 ms P50 and 1.5 s P95 for first
audio (R9-216).

Rule: **choose `AZURE_REGION` to minimise the round trip to Postgres first, and to Norway
second**, because the database trips repeat inside every turn while the user's media trip is
paid once per direction. Concretely:

**Decision: `germanywestcentral` (Azure Frankfurt).** OCI `eu-frankfurt-1` and Azure Germany West
Central are in the same metro, so the tunnel round trip should be a few milliseconds, paid many
times per turn. The cost is the user's media path: Frankfurt to Oslo is roughly 20 to 30 ms each
way instead of about 10 to 15 ms from Sweden Central, paid once per direction. Sweden Central or
Norway East would cut that but put a Frankfurt to Scandinavia round trip on every database query,
which is the worse trade. Confirm Dasv5 capacity in Germany West Central when provisioning.

Acceptance A6 records the measured VM to Postgres round trip through the tunnel (expect single
digit milliseconds; investigate anything above 10 ms), and A11 is the binding check.

## 11. Security summary

* Inbound only 80, 443, 7881/tcp, 7882/udp from the internet; 22 from `ADMIN_CIDRS` (4.1).
* 8001, 7880, Docker API and any database port never public (4.2).
* Containers: all capabilities dropped (Caddy keeps `NET_BIND_SERVICE`), pids limits,
  non-root users, LiveKit as 65534 on the host network (`deploy/hetzner/docker-compose.yml:203-204,247-251,262-266`).
* IMDS blocked from bridged containers; if a managed identity is attached (for Key Vault),
  scope it to read only the secrets this VM needs.
* The database link encrypted and private (4.4).
* No URI or header logging of LiveKit signaling anywhere (4.2).

## 12. Acceptance checklist (run on the provisioned VM)

Each item has a pass condition. Items A11 to A13 are the owner's operator legs; the
infrastructure is not accepted until they pass.

| # | Check | Pass |
|---|---|---|
| A1 | `uname -m`, `lsb_release -a`, `nproc`, `free -g` | `x86_64`, Ubuntu 24.04, 4 vCPU, about 16 GiB (or the agreed size) |
| A2 | From an outside host: TCP connect to 80, 443, 7881; `nc -u` probe to 7882 | open (7882 has no reply by design; A8 is the real UDP check) |
| A3 | From an outside host: TCP connect to 22 (from a non-admin address), 7880, 8001, 2375, 5432 | all refused or filtered |
| A4 | `https://VOICE_HOST/v1/voices` and `https://LIVEKIT_HOST/` with a trusted (production CA) certificate | 401 and 200; certificate valid, issuer Let's Encrypt production |
| A5 | `sudo ss -ulpn \| grep 7882`, `sudo ss -ltnp \| grep -E '7880\|7881\|8001'`, `sudo ufw status` | listening; 7880 and 8001 bound to 127.0.0.1 or host only and NOT allowed in ufw (`deploy/hetzner/RUNBOOK.md`, UDP checks 1 and 2) |
| A6 | `sudo wg show` (latest handshake under 2 minutes); from inside the voice container, time 20 `SELECT 1` round trips through the tunnel to `pglink` | handshake recent; median recorded in the acceptance notes, expected single digit ms; `pglink` is not reachable on the Oracle host's public address (test from outside) |
| A6b | Connected as `persona_voice`: `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user` and `SELECT pg_has_role(current_user, 'persona_app', 'MEMBER')` | `f, f` and `t`; a voice call then reads and writes normally (A9) |
| A7 | `docker compose logs livekit \| head` | `advertising node_ip=<the VM public IPv4>` (`packages/livekit/entrypoint.sh:45`) |
| A8 | A real call in Chrome with `chrome://webrtc-internals` open | the selected candidate pair's remote candidate is the VM IPv4, port 7882, protocol udp (`deploy/hetzner/RUNBOOK.md`, UDP check 4) |
| A9 | During A8: the persona greets and answers | proves the agent joined via hairpin to its own public address; if it never speaks, check hairpin first |
| A10 | Block 7882/udp in ufw, call again, then re-allow | candidate pair is tcp 7881 and audio still flows (`deploy/hetzner/RUNBOOK.md`, UDP check 5) |
| A11 | **R9-216 test call**: one real call, at least five turns, first audio latency per turn from the voice logs; `vmstat 1` during the call | P50 at or under 800 ms, P95 at or under 1.5 s; no turn hits the 20 s stall guard; CPU steal (`st`) near 0 (docs repo, `specs/phase3/spec_R9/issues.md` R9-216; `deploy/hetzner/RUNBOOK.md` step 11 item 3) |
| A12 | Concurrency: N_CALLS simultaneous calls (5 by default), each running A11's measurement | each call within the A11 budget; record CPU and memory at peak |
| A13 | **R9-265 VAD threshold check**, on this VM's CPU, from the repo checkout: `uv run pytest -m external packages/voice/tests/external/test_real_provider_smoke.py` with the provider keys loaded from the env file (never echoed); then confirm `PERSONA_STT_SILERO_ACTIVATION_THRESHOLD` and `PERSONA_STT_SILERO_MIN_SPEECH_DURATION_MS` by ear on a real call (barge-in works, no false starts on the persona's own audio) | gates 1, 3 and 4 pass or their falsification route is recorded (`test_real_provider_smoke.py:1-31`); the owner signs off the threshold values (docs repo, `specs/phase3/spec_R9/issues.md` R9-265). The sine-wave stimulus R9-265 complains about is already gone: gate 3 now feeds rendered speech from the V8 corpus (`test_real_provider_smoke.py:230-245`). The VAD itself now runs on onnxruntime (`persona_voice.stt.silero_onnx`), held to the old library's scores by `tests/unit/stt/test_silero_equivalence.py`; this check confirms it on real calls |
| A14 | A cold boot: delete the HF cache directory, restart voice | healthy within the 300 s start period, models re-downloaded |
| A15 | Reboot the VM | all three containers come back without manual steps; A4 and A8 pass again |
| A16 | From inside the voice container: `curl -m 3 http://169.254.169.254/` | fails (metadata blocked) |
| A17 | Caddy and any Azure log sink after A8 | no LiveKit token query strings and no Authorization headers in any log |
| A18 | The V6 formal voice operator pass, re-cut for this host (docs repo, `specs/phase2/spec_V6/operator_pass_script_2026_09.md`) | owner signs off |

## 13. Open items for the owner and the code side

1. Decided: `eu-frankfurt-1` and `germanywestcentral` (section 10); WireGuard plus `pglink` plus
   `persona_voice` (4.4). Open: which session owns the WireGuard peer on each side and the
   `persona_voice` role creation (the Oracle side, per the Oracle plan).
2. Where images are built: on the VM (needs 128 GB disk) or in CI with a registry (GHCR or ACR).
3. Code side, not Terraform: the `VOICE_HOST` body limit is 12 MB on `/v1/stt` only, on `feat/voice-arm` (section 5); add a
   voice and LiveKit only compose file and env templates for this VM (the single-box kit
   includes postgres and the api); consider moving torch to the CPU-only build to drop about
   2.2 GB (compressed) of unused CUDA libraries from the image (7.2).
4. Scale past one voice process is not designed; if N_CALLS must exceed what A12 shows, that is
   a code spec, not a bigger VM.
