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

> **Target change (2026-10-23):** the owner moved the target from Hetzner to Azure. The
> container kit here (compose, Caddy, the scripts, the env templates, the restore) is
> target-independent and carries over; the Hetzner-specific steps (the console, its
> cloud-init, its snapshots) are superseded by the Azure plan and are kept only as a
> reference until that lands.

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
- Turn on two-factor authentication on the cloud account and the DNS account before any
  production data is uploaded (step 5).

## What is in this directory

| file | role |
|---|---|
| `docker-compose.yml` | postgres (pgvector pg16, unpublished), migrate (one-shot), api (single instance), voice, livekit (profile `selfhosted-livekit`), caddy |
| `Caddyfile` | TLS for the five hostnames, Let's Encrypt, HTTP to HTTPS |
| `cloud-init.yaml` | first-boot baseline: `deploy` user, key-only SSH, ufw, fail2ban, unattended upgrades |
| `bin/provision.sh` | idempotent provisioning: the baseline again, Docker, app firewall ports, directories, backup timer |
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

- **Per-tenant MCP runtime: OFF** (`PERSONA_ALLOW_PER_TENANT_MCP=0`, no `FLY_API_TOKEN`,
  no `PERSONA_MCP_RUNTIME_FLY_APP`). It booted one Fly Machine per (tenant, server) through
  the Fly Machines API, and its Fly app is deleted. Users keep the built-in MCP surface.
  The boot line says `off (reason: unconfigured)`. OFF at launch only: the owner wants
  per-tenant MCP supported and done right, and the session that owns tooling and MCP
  builds the replacement. It plugs into the named seam (see "Seam: the per-tenant MCP
  runtime" below); this kit does not design it.
- **Initiative: OFF** until the A5-R-1 re-run is green and the owner says so. The api
  template pins `PERSONA_INITIATIVE_ENABLED=false` whatever `.env` says.
- **Hostnames are kept**, so no provider registration changes: Stripe webhook, Discord,
  Slack and GitHub OAuth redirects, Twilio and Postmark webhooks, Vercel's API and voice
  base URLs. Only DNS records change.
- **LiveKit UDP: single port 7882** (the proven config), with the TCP 7881 fallback.
- **LiveKit mode: the owner's call.** Both work without editing compose; see step 9.
- **Stripe stays in test mode**, as production ran. Going live is a separate decision.

## Owner inputs (checklist)

- [ ] A Hetzner server (step 1). No API token is needed: nothing here calls Hetzner.
- [ ] The SSH public key for `deploy` (and later a second key for CI, see the last section).
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
- [ ] LiveKit: Cloud (the project still exists: its URL, key and secret) or self-hosted.
- [ ] Later: an off-site backup target (a Hetzner Storage Box, or Oracle free block storage).

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

## Step 1: create the server (Hetzner console)

- **Type:** the CX42 class (8 vCPU, 16 GB, 160 GB), or its current equivalent. Names and
  prices may have changed since the plan was written; check on the day. CX is **shared**
  vCPU. If the R9-216 measurement shows CPU steal, the dedicated CCX line is the upgrade
  and Hetzner resizes in place. Do not go below 16 GB.
- **Location:** Falkenstein, Nuremberg or Helsinki.
- **Image:** Ubuntu 24.04, x86_64 (never ARM: `silero-vad-lite` has no arm64 wheel).
- **Networking:** a primary IPv4 (billed separately) and IPv6.
- **SSH key:** the owner's public key.
- **Cloud config:** paste `cloud-init.yaml` with `__OWNER_SSH_PUBLIC_KEY__` replaced.
- **Backups:** Hetzner's own backups are recommended (a percentage of the server price).
- **Firewall (optional, a second layer):** inbound 22/tcp, 80/tcp, 443/tcp, 7881/tcp,
  7882/udp. Nothing else. A TCP-only rule set looks healthy until no audio flows.

Note the IPv4 and IPv6 addresses.

## Step 2: put the code on the box

The box needs no GitHub credential for this. From the owner's PC, in the repo, with a
LOCAL branch that holds this kit (`main` once the Hetzner branch is merged, until then
`feat/hetzner-move`). A bundle of `origin/main` would not work: it carries a remote ref,
no branch, and the clone comes up empty.

```bash
BRANCH=main                                   # or feat/hetzner-move before the merge
git bundle create /tmp/open-persona.bundle "$BRANCH"
scp /tmp/open-persona.bundle deploy@<ip>:/tmp/
ssh deploy@<ip> "git clone -b $BRANCH /tmp/open-persona.bundle /opt/open-persona/repo && rm /tmp/open-persona.bundle"
rm /tmp/open-persona.bundle
# The clone's `origin` is the bundle file, now deleted. Point it at GitHub for later pulls:
ssh deploy@<ip> "git -C /opt/open-persona/repo remote set-url origin https://github.com/<owner>/<repo>.git"
```

On the first `ssh`, compare the host key fingerprint it shows with the one the server
console reports (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` in the provider's web
console). Do not upload anything until they match.

(Alternative: a read-only GitHub deploy key on the box and `git clone` over SSH. That is
what the CI deploy will need anyway, see the last section.)

## Step 3: provision

```bash
ssh deploy@<ip>
sudo /opt/open-persona/repo/deploy/hetzner/bin/provision.sh
```

It prints the firewall at the end: 22 (limited), 80, 443, 7881/tcp, 7882/udp. If it says
a reboot is required, reboot now. Then take a **snapshot** in the console
("provisioned, empty").

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
uv run python -I deploy/hetzner/render_env.py --dotenv .env --out ~/op-env \
    --livekit cloud            # or: --livekit selfhosted   (step 9)
    # add --chains-from-dotenv only after confirming .env holds the production chains
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
scp ~/op-env/*.env deploy@<ip>:/opt/open-persona/env/
ssh deploy@<ip>
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
scp ~/op-restore/* deploy@<ip>:/srv/open-persona/restore/
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
ssh -L 8000:127.0.0.1:8000 -L 8001:127.0.0.1:8001 deploy@<ip>
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

**Recommendation: LiveKit Cloud first, if the project still exists; self-hosted later as
a separate, measured step.** The self-hosted SFU was retired on 2026-09-03 after the first
good call on Cloud, and the greeting timeouts of that era (R9-116) were never isolated to
one cause. Going live on a new host AND a new SFU at once changes two variables; Cloud
first isolates the host. The Cloud free tier's minute cap remains the watch item.

**Cloud.** `render_env.py --livekit cloud` copied `.env`'s `PERSONA_LIVEKIT_URL/_API_KEY/
_API_SECRET` into voice.env's `PERSONA_VOICE_LIVEKIT_*`. Nothing runs on the box for it.
Check a call in the LiveKit Cloud dashboard.

**Self-hosted.** In `/opt/open-persona/env/voice.env` set
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

## Seam: the per-tenant MCP runtime

Per-tenant MCP is OFF at launch and comes back when the session that owns tooling and MCP
delivers its runtime. The seam is marked `SEAM: per-tenant MCP runtime` in
`docker-compose.yml`. The contract this kit holds that work to, nothing more:

- The runtime is its own compose service on the `backend` network, behind its own
  profile, so the stack without it is exactly today's.
- The api learns about it only from settings in `api.env`: `PERSONA_ALLOW_PER_TENANT_MCP`
  plus whatever settings the runtime defines. `fill_generated.sh`, `render_env.py` and the
  templates gain those names in the same change that adds the service.
- **The api never gets `/var/run/docker.sock`.** The api holds every tenant's data and
  credentials; host-level container control does not belong in that process. Whatever the
  runtime needs, it holds itself. `scripts/tests/test_hetzner_kit.py` fails if the api or
  voice service mounts the socket.
- Nothing it adds is published past 127.0.0.1, and ufw gains no rule for it.

Switching it on is then: the runtime's env, `PERSONA_ALLOW_PER_TENANT_MCP=1`, its compose
profile, `bin/deploy.sh`.

## Alerts

A failed nightly backup, a disk-usage check over 85 % (daily, `bin/diskcheck.sh`) or a
backup refused for lack of free space (`OP_BACKUP_MIN_FREE_GB`, default 20) is never
silent: the unit's `OnFailure=` runs `bin/alert.sh`, which

1. appends a line to `/var/lib/open-persona/alerts/ALERTS`, shown on every SSH login (the
   banner) and failed by `bin/preflight.sh` until acknowledged with `bin/alert.sh --ack`;
2. runs `OP_ALERT_CMD` from `alert.env` if the owner set one (a webhook or a mail
   command; examples in `env/alert.env.example`). The marker never depends on it.

## Backups off the box (owner-provided, soon after go-live; not built here)

A backup on the box's own disk does not survive the disk. The minimum design, to be
provided on the new target:

- **Encrypted on the box** before it leaves (`age` or `gpg` to a public key), with the
  decryption key kept OFF the box (the owner's password manager and one offline copy).
- **Append-only credentials**: the box can write new backups but cannot delete or overwrite
  old ones (an immutability or retention lock on the storage side), so a compromised box
  cannot erase history.
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
