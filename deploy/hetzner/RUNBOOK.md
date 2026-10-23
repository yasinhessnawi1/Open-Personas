# Open Persona on Hetzner: restore runbook

Production has had no host since 2026-10-02, when all six Fly apps were deleted. This
runbook brings it back on one Hetzner box by **restoring from backup**. There is no live
cutover and nothing to switch back to: the Fly apps, their volumes and their secrets are
gone. What exists is the backup set in `~/Backups/fly-2026-10-02` on the owner's PC:

| file | what it is |
|---|---|
| `persona.dump` | `pg_dump -Fc` of database `persona` (Postgres 16.14, pgvector) |
| `globals.sql` | the roles `persona` and `persona_app` (old password hashes, never replayed) |
| `persona_volume.tgz` | the api volume `/var/lib/persona` without the HF model cache |
| `secret_names.txt` | Fly secret NAMES (api only; voice and livekit came out as `*`) |

Treat the first three as production user data: never paste their contents anywhere, never
commit them, delete the copy on the box once the move is signed off.

**Never paste the output of `docker compose config`, `printenv`, `env` or
`docker inspect` anywhere** (a chat, an issue, a log paste): each prints every secret the
service holds. The same goes for `pg_restore` error output, which can echo row data.

## Who can do what

- **`deploy` is root-equivalent**: it has passwordless sudo and is in the `docker` group,
  either of which is root on the box. Its SSH key IS the key to production. Use a hardware
  key (`ssh-keygen -t ed25519-sk`, FIDO2) or at least a passphrase-protected key that never
  leaves the owner's machine.
- `deploy` is uid 2000. The app's data is owned by uid 1000 (the images' `persona` user),
  so the operator's login does not own it; the nightly backup runs as root.
- **Database roles.** `persona` (superuser) is used only by the one-shot `migrate` service
  (`migrate.env`, read by nothing else) and by the restore scripts. The api's cross-tenant
  engines use `persona_platform` (bypasses RLS, NOT a superuser, DML only: it cannot
  `COPY ... TO PROGRAM`, alter roles or create tables). Every request path, api and voice,
  uses `persona_app` (RLS enforced). `bin/preflight.sh` checks all three.
- Turn on two-factor authentication on the Hetzner account and the DNS account before
  any production data is uploaded (step 5).

## What is in this directory

| file | role |
|---|---|
| `docker-compose.yml` | postgres (pgvector pg16, unpublished), migrate (one-shot), api (single instance), voice, livekit (profile `selfhosted-livekit`), caddy |
| `Caddyfile` | TLS for the five hostnames, Let's Encrypt, HTTP to HTTPS |
| `bin/provision.sh` | THE provisioning path: run once as root over SSH; creates `deploy` with root's key, then SSH lockdown, ufw, fail2ban, upgrades, Docker, directories, timers. Idempotent |
| `cloud-init.yaml` | the alternative path: the same baseline at first boot, then `provision.sh` as usual |
| `render_env.py` | owner's PC: turns `env/*.env.example` + the local `.env` into the env files, printing names only |
| `env/*.env.example` | every variable each service needs, with its source |
| `bin/fill_generated.sh` | on the box: generates the new database passwords, the LiveKit key pair, the node IP |
| `bin/restore.sh` | on the box: roles, pgvector, `pg_restore`, verification, the api volume |
| `bin/deploy.sh` | on the box: build, pre-migrate dump, migrate, start, health checks |
| `bin/preflight.sh` | on the box: env completeness, firewall, DNS, listeners |
| `bin/backup.sh` + `systemd/` | nightly dump, roles, api data, facts, checksums; refuses below a free-space floor |
| `bin/alert.sh`, `bin/diskcheck.sh` | a failed backup or a full disk raises an alert (marker, login banner, optional notifier) |
| `sql/verify.sql` | the post-restore facts (counts and schema facts only) |

Tests: `scripts/tests/test_hetzner_kit.py` (runs in CI with every other script test).

## Rulings already made

- **Per-tenant MCP runtime: OFF at launch** (`PERSONA_ALLOW_PER_TENANT_MCP=0`, no
  `FLY_API_TOKEN`, no `PERSONA_MCP_RUNTIME_FLY_APP`). It booted one Fly Machine per
  (tenant, server) through the Fly Machines API, and its Fly app is deleted. Users keep
  the built-in MCP surface; the boot line says `off (reason: unconfigured)`. The owner's
  direction: a new spec uses provider-hosted MCP (the model providers call remote MCP
  servers), so no container runtime is planned on this box. See "Seam: per-tenant MCP".
- **Initiative: OFF** until the A5-R-1 re-run is green and the owner says so. The api
  template pins `PERSONA_INITIATIVE_ENABLED=false` whatever `.env` says.
- **Hostnames are kept**, so no provider registration changes: Stripe webhook, Discord,
  Slack and GitHub OAuth redirects, Twilio and Postmark webhooks, Vercel's API and voice
  base URLs. Only DNS records change.
- **LiveKit UDP: single port 7882** (the proven config), with the TCP 7881 fallback.
- **LiveKit is SELF-HOSTED on the box** (owner ruling 2026-10-23), with LiveKit Cloud's
  free plan as a one-setting fallback (step 9). `render_env.py` defaults to it. The box is
  sized with a core's headroom for it, and the R9-216 latency test is the move's sign-off.
- **Backups:** Hetzner Backups ON (step 1, mandatory), the nightly on-box backups, and an
  encrypted append-only off-site copy soon after go-live (owner-provided).
- **Stripe stays in test mode**, as production ran. Going live is a separate decision.

## Owner inputs (checklist)

- [ ] A Hetzner server (step 1), created with only the owner's SSH key on root and
      Hetzner Backups ON. No API token is needed: nothing here calls Hetzner.
- [ ] Its IPv4 address and IPv6 /64, and SSH access as root from this PC with that key
      (the key loaded in the ssh agent, or its file path).
- [ ] DNS access to `openpersonasai.com` and `openpersona.online`, and the full record list.
- [ ] The `render_env.py --inventory` checklist (step 4) marked `[p]` or `[d]` per name, and
      the production value for every `[d]`. `.env` holds production values for only some
      names. Look hardest at:
      `PERSONA_API_JWT_PUBLIC_KEY` and `PERSONA_CONNECTORS_JWT_*` (the production Clerk
      instance, not a `*.clerk.accounts.dev` key), `MCP_CREDENTIAL_KEY` (must be the exact
      production key or stored MCP credentials become unreadable), the Stripe test keys and
      webhook secret, every connector bot token (a dev process polling the production
      Telegram bot steals its updates), and the behaviour flags marked "confirm".
- [ ] The seven shared settings (`PERSONA_*_MODELS` and
      `PERSONA_OPENROUTER_SUBSCRIPTION_MODE`). GitHub cannot read a secret back, so either
      supply the Production environment's values or confirm `.env` holds them.
- [ ] The web origin(s) for `PERSONA_API_CORS_ORIGINS` and `PERSONA_VOICE_CORS_ORIGINS`
      (expected `https://app.openpersonasai.com`, plus any `openpersona.online` origin).
- [ ] An email address for Let's Encrypt notices (`ACME_EMAIL`).
- [ ] Only if the LiveKit fallback is ever used: the LiveKit Cloud free-plan project's URL,
      key and secret (kept in the owner's password manager, not on the box).
- [ ] Soon after go-live: the off-site backup target (a Hetzner Storage Box sub-account
      is the suggestion; see "Backups off the box").

## Step 0: close the subdomain takeover (DONE 2026-10-23)

**Done by the owner on 2026-10-23: the dangling records are removed.** Kept for the
record and for any record found later. The production records were CNAMEs to `*.fly.dev`
names (`open-persona-api.fly.dev` and siblings) whose apps were deleted. Those names are
free: anyone can create a Fly app with that name, add a certificate for our hostname and
serve content on it. Any record that still names `*.fly.dev` must go:

```
api.openpersonasai.com         connectors.openpersonasai.com
voice.openpersonasai.com       livekit.openpersonasai.com
api.openpersona.online         (and any other record that names *.fly.dev)
```

At step 8 the records come back as A/AAAA records pointing at the box; create them with
a TTL of 300 seconds and raise it once stable.

## Step 1: create the server (Hetzner console, the owner)

In Hetzner Cloud console > the project > Add Server:

| setting | choose |
|---|---|
| Location | **Helsinki (hel1)**: the closest to Norway. Falkenstein or Nuremberg also work |
| Image | **Ubuntu 24.04** |
| Type | **Shared vCPU, x86 (Intel/AMD), the CX42 class: 8 vCPU, 16 GB RAM, 160 GB disk** (or its current successor with the same shape). NOT the Arm (CAX) line: `silero-vad-lite` has no arm64 wheel. Not below 16 GB. If the R9-216 test shows CPU steal, resize in place to the dedicated CCX line |
| Networking | **Public IPv4 ON** (billed separately) and **Public IPv6 ON** |
| SSH keys | **only the owner's public key** (it lands on root) |
| Volumes | none |
| Firewalls | optional second layer; if used, inbound only 22/tcp, 80/tcp, 443/tcp, 7881/tcp, 7882/udp (a TCP-only set looks healthy until no audio flows) |
| **Backups** | **ON (mandatory)**: Hetzner's daily server backups, about 20 % on top of the server price. The rollback for the whole box |
| Placement groups, Labels | none needed |
| Cloud config | **leave empty** (the primary path is `provision.sh`; `cloud-init.yaml` is the alternative) |
| Name | e.g. `open-persona-prod` |

Prices and type names can change; check on the day. Then send: the **IPv4**, the **IPv6
/64** (the box uses `<prefix>::1`), and confirm `ssh root@<ipv4>` works from this PC.

**Host key check, before anything is uploaded:** in the console, "Rescue > Reset root
password", open the web console (">_"), log in as root and run
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`; the first `ssh root@<ipv4>` must show
the same fingerprint.

## Step 2: put the code on the box (as root)

The box needs no GitHub credential. From the owner's PC, in the repo, with a LOCAL branch
that holds this kit (`main` once merged, until then `feat/hetzner-move`). A bundle of
`origin/main` would not work: it carries a remote ref, no branch, and clones empty.

```bash
BRANCH=main                                   # or feat/hetzner-move before the merge
git bundle create /tmp/open-persona.bundle "$BRANCH"
scp /tmp/open-persona.bundle root@<ipv4>:/tmp/
ssh root@<ipv4> "apt-get update -q && apt-get install -y -q git \
  && mkdir -p /opt/open-persona \
  && git clone -b $BRANCH /tmp/open-persona.bundle /opt/open-persona/repo \
  && rm /tmp/open-persona.bundle"
rm /tmp/open-persona.bundle
```

## Step 3: provision (as root, once)

```bash
ssh root@<ipv4> /opt/open-persona/repo/deploy/hetzner/bin/provision.sh
```

It creates `deploy` (uid 2000) with root's SSH key, writes the SSH lockdown (no root
login, keys only, only `deploy`), then ufw (22 limited, 80, 443, 7881/tcp, 7882/udp),
fail2ban, unattended upgrades, Docker (signing key fingerprint checked), the directories,
the backup, disk-check and alert units. The root session that ran it stays open: **before
closing it, check from a second terminal** that `ssh deploy@<ipv4>` works and
`sudo -n true` succeeds. From then on everything runs as `deploy`; a later re-run is
`sudo /opt/open-persona/repo/deploy/hetzner/bin/provision.sh`.

Point the checkout at GitHub for later pulls (its `origin` is the deleted bundle):

```bash
ssh deploy@<ipv4> "git -C /opt/open-persona/repo remote set-url origin https://github.com/<owner>/<repo>.git"
```

If it says a reboot is required, reboot now. Then take a console **snapshot**
("provisioned, empty").

**Alternative path:** paste `cloud-init.yaml` (with `__OWNER_SSH_PUBLIC_KEY__` replaced)
into "Cloud config" at step 1. It creates `deploy` and the same baseline at first boot;
then steps 2 and 3 run as `deploy` with `sudo`.

## Step 4: the env files

On the **owner's PC**, from the repo. First the inventory: it writes nothing and lists,
for every name the render would take from `.env`, whether `.env` has a value (`set`,
`empty` or `absent`, never the value) and which env file it goes to. `.env` holds
production values for only some names, so mark each line `[p]` or `[d]` and have the
production value ready for every `[d]` before rendering:

```bash
uv run python -I deploy/hetzner/render_env.py --dotenv .env --inventory > ~/op-inventory.txt
```

Then render, into a directory OUTSIDE the repo:

```bash
uv run python -I deploy/hetzner/render_env.py --dotenv .env --out ~/op-env
# LiveKit defaults to selfhosted (owner ruling); add --livekit cloud only for the fallback
# (step 9). Add --chains-from-dotenv only after confirming .env holds the production chains.
```

It prints every name with its state, never a value. Then edit the files in `~/op-env`:

- `__OWNER__`: fill in.
- `__GITHUB_PRODUCTION__`: fill in the Production environment's value.
- `__MISSING_FROM_DOTENV__`: supply the value, or **delete the line** if production never
  set it (the code default then applies, exactly as on Fly).
- Every name marked `[d]` in the inventory: replace the copied dev value with production's.
- `__OWNER_CLERK_PROD__`: every Clerk-derived value. `.env` holds the DEVELOPMENT Clerk
  instance's, so none is copied (owner ruling 2026-10-23). In the Clerk dashboard, switch to
  the **production** instance (Frontend API `clerk.openpersona.online`), then:
  - `PERSONA_API_JWT_PUBLIC_KEY`, `PERSONA_VOICE_JWT_PUBLIC_KEY` and
    `PERSONA_CONNECTORS_JWT_PUBLIC_KEY`: Configure > API keys > the JWT public key, PEM
    format. The same PEM in all three.
  - `PERSONA_CONNECTORS_JWT_JWKS_URL`: the JWKS URL on the same page
    (`https://clerk.openpersona.online/.well-known/jwks.json`).
  - `PERSONA_CONNECTORS_JWT_SECRET`: the HS256 development path; delete the line in
    production (the connectors verify RS256 with the PEM above).
  - Check under Configure > JWT templates that the production instance has the
    `persona-api` template with `aud` = `persona-api` (the audience the compose file pins).
  `bin/preflight.sh` blocks until every one is pasted, and refuses any value naming
  `*.clerk.accounts.dev`.

Copy them to the box over SSH only (scp; never a chat, mail or shared drive), and finish
them there:

```bash
scp ~/op-env/*.env deploy@<ipv4>:/opt/open-persona/env/
ssh deploy@<ipv4>
cd /opt/open-persona/repo/deploy/hetzner
bin/fill_generated.sh          # new DB passwords (3 roles), LiveKit key pair, node IP
bin/preflight.sh --env-only    # must pass: roles separated, no dev Clerk instance
```

`/opt/open-persona/env/.generated` now holds the only copy of the new database passwords.
Put the whole `/opt/open-persona/env` directory in the owner's password manager. Delete
`~/op-env` from the PC afterwards (`shred -u ~/op-env/*.env` where available).

`alert.env` decides where alerts go besides the on-box marker (see "Alerts" below).

## Step 5: restore

On the owner's PC, checksum a COPY of the set (never write into the original directory),
then upload:

```bash
mkdir -p ~/op-restore && cp ~/Backups/fly-2026-10-02/{persona.dump,globals.sql,persona_volume.tgz} ~/op-restore/
(cd ~/op-restore && sha256sum persona.dump globals.sql persona_volume.tgz > SHA256SUMS)
scp ~/op-restore/* deploy@<ipv4>:/srv/open-persona/restore/
rm -rf ~/op-restore
```

On the box:

```bash
cd /opt/open-persona/repo/deploy/hetzner
bin/restore.sh --backup-dir /srv/open-persona/restore
```

What a correct restore prints (from the 2026-10-23 rehearsal against this exact set):

```
tables|54   forced_rls_tables|45   policies|46
persona_app_superuser|false   persona_app_bypassrls|false
persona_app_tables_without_select|0
persona_platform_superuser|false   persona_platform_tables_without_dml|0
vector_columns|4   hnsw_indexes|3
alembic_revision|058_jobs_live_task_leg_index
RLS check passed: persona_app with no user set sees 0 personas
platform check passed: persona_platform has no DDL
volume archive scan passed: regular files and directories only
volume entries: 214 (the backup listed 214)
```

Anything else stops the script. `pg_restore` runs in one transaction, so a failed restore
leaves no tables behind, and the role and extension steps are idempotent: fix the cause
and run `bin/restore.sh` again. If the failure came AFTER `pg_restore` (a verification
check), the tables are there and the script refuses to restore over them; start clean with
`docker compose -f docker-compose.yml down` then `docker volume rm open-persona-hetzner_pgdata`.
**Only before go-live:** once real users write to this database, that command destroys
production. After go-live a failed step is fixed in place, never by dropping the volume.

The volume is unpacked as uid 1000, never as root, after a scan that refuses any symlink,
hardlink, device node, absolute path or `..` path; no owners or modes come from the
archive. `bin/restore.sh --roles-only` re-applies the roles and grants to an existing
database (a password rotation), touching no data.

## Step 6: deploy

```bash
bin/deploy.sh
```

Before the first `deploy.sh`, pin Caddy by digest (the kit could not fetch it offline):
`docker pull caddy:2.10.2 && docker image inspect caddy:2.10.2 --format '{{index .RepoDigests 0}}'`,
then commit that digest into `docker-compose.yml` as `caddy:2.10.2@sha256:...`.
`deploy.sh` validates the Caddyfile before every start (Caddy's admin API is off, so a
config change only ever lands through a restart).

The first build takes a while (both images carry CUDA torch wheels, several GB each). The
migrate step is expected to be a **no-op** on today's main: the dump is already at head
(`058`). The first boot downloads the HF models (about 2 GB) into the volumes, hence the
long health timeouts. `deploy.sh` ends with `api /livez: ok`, `api /healthz (database):
ok` and `voice listener: ok`. The Caddy smoke test warns until DNS points here.

Before DNS, check through an SSH tunnel from the PC:

```bash
ssh -L 8000:127.0.0.1:8000 -L 8001:127.0.0.1:8001 deploy@<ipv4>
curl -fsS http://127.0.0.1:8000/healthz
docker compose -f /opt/open-persona/repo/deploy/hetzner/docker-compose.yml logs api | grep -iE "model chain|mcp runtime|connectors"
```

The api boot log should show the model chains, the MCP startup summary with the
per-tenant runtime `off (reason: unconfigured)`, and the connector transports starting.

## Step 7: snapshot

Console snapshot "restored, not yet public". This is the move's rollback point.

## Step 8: DNS and certificates

Create A (and AAAA) records, **DNS only** (if the zone is on Cloudflare, grey cloud: a
proxied record cannot carry LiveKit's UDP and hides client addresses):

```
api.openpersonasai.com           A  <ipv4>   AAAA <ipv6>
api.openpersona.online           A  <ipv4>   AAAA <ipv6>
connectors.openpersonasai.com    A  <ipv4>   AAAA <ipv6>
voice.openpersonasai.com         A  <ipv4>   AAAA <ipv6>
livekit.openpersonasai.com       A  <ipv4>   (keep it in both LiveKit modes)
```

Then on the box: `bin/preflight.sh` (all five names should resolve here) and watch Caddy
obtain STAGING certificates: `docker compose -f docker-compose.yml logs -f caddy`. When
every name has one, switch to production certificates:

```bash
sed -i 's|^ACME_CA=.*|ACME_CA=https://acme-v02.api.letsencrypt.org/directory|' /opt/open-persona/env/caddy.env
docker compose -f docker-compose.yml up -d --force-recreate caddy
curl -fsS https://api.openpersonasai.com/healthz      # from anywhere
```

Once stable for a day, raise the TTLs back to 3600.

## Step 9: LiveKit

**Self-hosted on this box is the owner's ruling (2026-10-23)**; LiveKit Cloud's free plan
is the one-setting fallback. History to keep in mind while testing: the self-hosted SFU on
Fly was retired on 2026-09-03 after the first good call on Cloud, and the greeting
timeouts of that era (R9-116) were never isolated to one cause. If a call misbehaves,
switching to the fallback for one test call separates the SFU from the rest.

**Fallback: Cloud.** `render_env.py --livekit cloud` copies `.env`'s `PERSONA_LIVEKIT_URL/_API_KEY/
_API_SECRET` into voice.env's `PERSONA_VOICE_LIVEKIT_*`. Nothing runs on the box for it.
Check a call in the LiveKit Cloud dashboard.

**Self-hosted (the default).** `render_env.py` already rendered it; to return to it from
the fallback, in `/opt/open-persona/env/voice.env` set
`PERSONA_VOICE_LIVEKIT_URL='wss://livekit.openpersonasai.com'`,
`PERSONA_VOICE_LIVEKIT_API_KEY='__GEN_LIVEKIT_API_KEY__'` and
`PERSONA_VOICE_LIVEKIT_API_SECRET='__GEN_LIVEKIT_API_SECRET__'` (or re-render with
`--livekit selfhosted`), make sure `livekit.env` is present, then
`bin/fill_generated.sh && bin/deploy.sh --skip-build`. `deploy.sh` sees the URL and starts
the `selfhosted-livekit` profile. Then the UDP checks, in order:

1. `sudo ss -ulpn | grep 7882` and `sudo ss -ltnp | grep -E '7880|7881'`: listening.
2. `sudo ufw status`: 7881/tcp and 7882/udp allowed, 7880 NOT allowed.
3. `docker compose -f docker-compose.yml logs livekit | head`: `advertising node_ip=<the box IPv4>`.
4. A real call in Chrome with `chrome://webrtc-internals` open: the selected candidate
   pair's remote candidate is the box IPv4, port 7882, protocol **udp**. A listening
   socket proves nothing; this is the check.
5. TCP fallback: `sudo ufw delete allow 7882/udp`, call again, the pair is now tcp 7881
   and audio still flows; then `sudo ufw allow 7882/udp`.

Known limit, unchanged from Fly: no TURN, so a network that allows only outbound 443 TLS
cannot carry media.

Switching back to Cloud is the same three lines in voice.env and
`bin/deploy.sh --skip-build`.

## Step 10: verify the product

- Sign in on the web app; open a conversation (history from the restore is there); send a
  message and watch it stream (SSE through Caddy).
- An uploaded image or document from before the move opens (the workspace volume).
- Telegram (and any other linked channel): a message round trip.
- Stripe dashboard (test mode): send a test event to
  `https://api.openpersonasai.com/v1/billing/webhook`; it returns 200.
- MCP: an existing GitHub OAuth connection still works (proves `MCP_CREDENTIAL_KEY`).
- `bin/preflight.sh` passes in full.

## Step 11: the operator passes (owner ruling 2026-09-26)

1. **V6 formal voice pass** (`specs/phase2/spec_V6/operator_pass_script_2026_09.md` in the
   docs repo), re-cut for this host's voice service and the chosen LiveKit mode.
2. **Stripe test-mode pass** for the auto top-up invoice chain (R9-177): a card crossing the
   $2 line, a retry within the hour, a 3DS card, a decline card.
3. **R9-216:** one real call measured against the 800 ms P50 voice budget. **The move is
   not done until it passes.** During the call, watch CPU steal on the box
   (`vmstat 1`, column `st`); sustained steal is the case for the dedicated CCX line.

## Step 12: sign-off

- Delete `/srv/open-persona/restore` on the box (production data, no longer needed there).
- Delete the "restored, not yet public" snapshot from step 7: it holds the database and
  secrets as they were before go-live, and is no longer the rollback once users write.
- Optional: CAA records (`0 issue "letsencrypt.org"`) on both domains, so no other CA can
  issue for our hostnames.
- Confirm the nightly backup ran (`systemctl list-timers open-persona-backup.timer`,
  `ls /srv/open-persona/backups/nightly`) and set up the off-site copy (`OFFSITE_CMD` in
  `systemd/open-persona-backup.service`).
- Snapshot "live".
- Initiative stays OFF until the A5-R-1 re-run.

## Seam: per-tenant MCP

Per-tenant MCP is OFF at launch. The coming spec uses **provider-hosted MCP**: the model
providers call remote MCP servers themselves, so nothing new needs to run on this box and
no container runtime is planned. The seam is marked `SEAM: per-tenant MCP runtime` in
`docker-compose.yml`, and whatever mechanism comes is held to this, nothing more:

- It reaches the api only through settings in `api.env` (`PERSONA_ALLOW_PER_TENANT_MCP`
  plus its own), added to the templates, `render_env.py` and preflight in the same change.
- **The api never gets the Docker socket**, `/run` or `/var/run` mounts, `privileged` or
  `DOCKER_HOST`. The api holds every tenant's data and credentials.
  `scripts/tests/test_hetzner_kit.py` fails on any of these, for every service.
- If it ever needs an endpoint WE host for providers to call, that is a new public route
  through Caddy with its own auth and request limits, and a firewall review. None is
  planned.

## Alerts

A failed nightly backup, a disk-usage check over 85 % (daily, `bin/diskcheck.sh`) or a
backup refused for lack of free space (`OP_BACKUP_MIN_FREE_GB`, default 20) is never
silent: the unit's `OnFailure=` runs `bin/alert.sh`, which

1. appends a line to `/var/lib/open-persona/alerts/ALERTS`, shown on every SSH login (the
   banner) and failed by `bin/preflight.sh` until acknowledged with `bin/alert.sh --ack`;
2. runs `OP_ALERT_CMD` from `alert.env` if the owner set one (a webhook or a mail
   command; examples in `env/alert.env.example`). The marker never depends on it.

## Backups off the box (owner-provided, soon after go-live; not built here)

Hetzner Backups (step 1) restore the whole server but live in the same provider account.
A backup on the box's own disk does not survive the disk. The minimum off-site design,
with a **Hetzner Storage Box** as the suggested target:

- A dedicated Storage Box **sub-account** for this box, limited to its own directory and
  reached over SFTP/rsync with its own SSH key.
- **Storage Box snapshots** taken on a schedule by the MAIN account (14 to 30 kept). The
  sub-account cannot delete them, so a compromised box that deletes its copies cannot
  erase history: that is the append-only property.

- **Encrypted on the box** before it leaves (`age` or `gpg` to a public key), with the
  decryption key kept OFF the box (the owner's password manager and one offline copy).
- The box's credential is the sub-account key only; it never holds the main account's.
- **14 to 30 days** of nightly sets (`persona.dump`, `globals.sql`, `persona_volume.tgz`,
  `facts.txt`, `SHA256SUMS`).
- **One test restore** into a throwaway database (`bin/restore.sh --container ...
  --db-only`) after setup, and again after any change to the pipeline.
- Wired through `OFFSITE_CMD` in `systemd/open-persona-backup.service`; a failing copy
  fails the unit and raises an alert.

## Rolling back

**The move:** there is no Fly to go back to. The rollback is the console snapshot from
step 7 (or step 3 for a clean box) plus the untouched backup set on the PC.

**A deploy:** `deploy.sh` tags the images it replaces as `:previous` and dumps the
database before migrating (`/srv/open-persona/backups/pre-deploy-*.dump`).

```bash
for i in api voice livekit; do docker image tag open-persona/$i:previous open-persona/$i:current 2>/dev/null; done
docker compose -f docker-compose.yml up -d
```

If that deploy ran a migration the old code cannot live with, restore the pre-deploy dump
into an emptied database with `pg_restore` (the same commands `bin/restore.sh` uses) before
starting the old images.

**Losing the box:** a new box, steps 1 to 6, with the newest off-site nightly backup as
`--backup-dir`. A nightly backup carries `facts.txt`, so `restore.sh` checks the schema
shape against that night's own facts and warns on any table whose row count moved.

## Routine operations

| on Fly | here (from `/opt/open-persona/repo/deploy/hetzner`) |
|---|---|
| `flyctl ssh console -a open-persona-api` | `docker compose -f docker-compose.yml exec api bash` |
| `flyctl ssh console -C "printenv NAME"` | `docker compose -f docker-compose.yml exec api printenv NAME` (prints a secret: never paste it) |
| `flyctl logs -a open-persona-voice` | `docker compose -f docker-compose.yml logs -f voice` |
| `flyctl status` | `docker compose -f docker-compose.yml ps` |
| release_command | `docker compose -f docker-compose.yml run --rm migrate` |
| `flyctl secrets set` | edit `/opt/open-persona/env/<service>.env`, then `docker compose -f docker-compose.yml up -d <service>` |
| `fly scale count` | not applicable: one api, one voice, by design |

- **Updates:** security updates install by themselves; the box never reboots by itself.
  When `/var/run/reboot-required` exists, reboot in a quiet hour (`sudo reboot`; every
  service has `restart: unless-stopped`).
- **Disk:** `docker system df`; old images: `docker image prune` (keeps `:current` and
  `:previous` because they are tagged).
- **A new release:** `cd /opt/open-persona/repo && git pull` (or a new bundle), then
  `deploy/hetzner/bin/deploy.sh`.

## Later: CI/CD to this box (sketch, not wired)

`.github/workflows/deploy.yml` still targets Fly and is manual-only since b53fed43. The
recommended replacement:

- **Trigger:** `workflow_dispatch` from `main` only, `environment: production`, the same
  fork and branch gates the current workflow and `scripts/check_deploy_wiring.py` enforce.
- **Job 1** is the existing `validate-model-chains` (holds the seven settings, no box key).
- **Job 2, `deploy-hetzner`**, holds three Production environment secrets:
  `HETZNER_SSH_KEY` (a dedicated ed25519 key), `HETZNER_KNOWN_HOSTS` (the box's pinned host
  key) and `HETZNER_HOST`. It writes the key to a 0600 file and runs
  `ssh deploy@$HETZNER_HOST <commit sha>` with the seven settings on stdin. Nothing else.
- **On the box**, that key's `authorized_keys` line is
  `command="/opt/open-persona/bin/ci-deploy",restrict ssh-ed25519 ...`: the key can run
  exactly one script. `ci-deploy` validates the SHA (40 hex), fetches and checks it out
  (a read-only GitHub deploy key on the box if the repository is private), writes the seven
  settings into `common.env`, and runs `deploy.sh`. It reads exactly seven `NAME=value`
  lines from stdin, accepts only the seven known names, rejects any value containing a
  newline, a carriage return, a quote or a `$`, and checks each value against a strict
  per-name pattern (a comma list of `provider/model` slugs; `free` or `paid` for the
  mode) before writing anything.
- **Build on the box, not in GitHub Actions + GHCR, for now.** The images carry CUDA torch
  (15 `nvidia-*` packages in `uv.lock`) and are several GB each: GHCR storage for a private
  repository beyond the free quota costs money and every deploy would push and pull GBs.
  A native build on 8 vCPU takes minutes, and the only new secret class is one SSH key.
  The cost is build CPU on the live box: the build runs inside dockerd, so nothing in
  `deploy.sh` can lower its priority, and deploys must run outside call hours. (If that
  ever bites, `systemctl set-property docker.service CPUWeight=...` is the lever to test.)
- **Revisit GHCR** when the images shrink (a CPU-only torch index) or a second box
  (staging) exists; then build once in Actions, push by digest, and `docker compose pull`
  on the box.
