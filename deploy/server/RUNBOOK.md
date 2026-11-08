# Open Persona on the Oracle core host: restore runbook

Production has had no host since 2026-10-02, when all six Fly apps were deleted. The owner
ruled on 2026-11-07 that it comes back on **two hosts**:

- the **core host**, an Oracle Cloud Always Free server (VM.Standard.A1.Flex, Ampere arm64,
  2 OCPU, 12 GB RAM, eu-frankfurt-1, Ubuntu 24.04), runs everything except voice: Postgres
  with pgvector, the api (with its in-process worker and the connector transports), Caddy,
  the migrate step, backups and alerts;
- the **voice host**, on Azure (provisioned by the Terraform session), runs voice and its
  LiveKit SFU later. See "Phase B" at the end.

This runbook brings the core host up by **restoring from backup**. There is no live
cutover and nothing to switch back to. What exists is the backup set in
`~/Backups/fly-2026-10-02` on the owner's PC:

| file | what it is |
|---|---|
| `persona.dump` | `pg_dump -Fc` of database `persona` (Postgres 16.14, pgvector) |
| `globals.sql` | the roles `persona` and `persona_app` (old password hashes, never replayed) |
| `persona_volume.tgz` | the api volume `/var/lib/persona` without the HF model cache |
| `secret_names.txt` | Fly secret NAMES (api only; voice and livekit came out as `*`) |

Treat the first three as production user data: never paste their contents anywhere, never
commit them, delete the copy on the host once the move is signed off.

**Never paste the output of `bin/compose.sh config`, `printenv`, `env` or `docker inspect`
anywhere** (a chat, an issue, a log paste): each prints every secret the service holds.
The same goes for `pg_restore` error output, which can echo row data.

**The host is shared with the owner's other work**, about 21 GB under `/home/ubuntu`
(runs, review-scratch, pinned, .cache). Nothing in this kit reads, moves or deletes
anything there, and no step below may either.

## Who can do what

- **`ubuntu`** (uid 1000) is the owner's login and keeps passwordless sudo. The kit does
  not lock it out: it stays in the SSH allow list.
- **`deploy`** (uid 2000) is the operator `provision.sh` creates, with ubuntu's SSH key. It
  is **root-equivalent** (passwordless sudo and the `docker` group): its key IS the key to
  production. Use a hardware key (`ssh-keygen -t ed25519-sk`) or at least a
  passphrase-protected key that never leaves the owner's machine.
- **The app's data belongs to uid 2001**, the data uid: the api and migrate containers run
  as it and `/srv/open-persona/persona` is owned by it. Not 1000: on this host that is
  `ubuntu`. Not 2000: that is the operator. The nightly backup runs as root.
  **What this separation does and does not do:** it guards against accidents (a script of
  the owner's other work, running as ubuntu, cannot read or overwrite tenant files by
  mistake). It does NOT guard against anything that runs as ubuntu on purpose: ubuntu has
  passwordless sudo, so ubuntu, and every process running as it, is root-equivalent and can
  read everything on this host. Treat the owner's other work as trusted code, or move it
  off this host (the owner is deciding, 2026-11-07).
- **Database roles.** `persona` (superuser) is used only by the one-shot `migrate` service
  (`migrate.env`, read by nothing else) and by the restore scripts. The api's cross-tenant
  engines use `persona_platform` (bypasses RLS, NOT a superuser, DML only: it cannot
  `COPY ... TO PROGRAM`, alter roles or create tables). The api's request path uses
  `persona_app` (RLS enforced). `bin/preflight.sh` checks all three.
- Turn on two-factor authentication on the Oracle Cloud account and the DNS account before
  any production data is uploaded (step 5).

## What is in this directory

| file | role |
|---|---|
| `docker-compose.yml` | the CORE host: postgres (pgvector pg16, unpublished, internal network only), migrate (one-shot), api (single instance, uid 2001), caddy |
| `docker-compose.voice.yml`, `Caddyfile.voice` | the VOICE host (Phase B): voice, livekit, its own Caddy. No profile deploys them yet |
| `hosts/oracle.env` | the core host's profile: memory budget, Postgres sizes, data uid, block volume, SSH allow list, Oracle specifics. Plain, non-secret, tracked |
| `Caddyfile` | TLS for the two core hostnames (from the profile), Let's Encrypt, HTTP to HTTPS, admin API off |
| `bin/provision.sh` | provisions and hardens the host from its profile. Idempotent |
| `bin/lib/profile.sh`, `bin/lib/compose.sh`, `bin/lib/oracle_firewall.sh` | the profile loader, the one way scripts reach compose, Oracle's firewall handed to ufw |
| `bin/compose.sh` | Compose for this host with the profile applied: every manual compose command below goes through it |
| `render_env.py` | owner's PC: turns `env/*.env.example` + the local `.env` into the env files, printing names only |
| `env/*.env.example` | every variable each service needs, with its source |
| `bin/fill_generated.sh` | on the host: generates the new database passwords |
| `bin/restore.sh` | on the host: roles, pgvector, `pg_restore`, verification, the api volume |
| `bin/deploy.sh` | on the host: build, pre-migrate dump, migrate, start, health checks |
| `bin/preflight.sh` | on the host: env completeness, the host as provisioned, firewall, listeners, DNS |
| `bin/backup.sh` + `systemd/` | nightly dump, roles, api data, facts, checksums; refuses below a free-space floor; off-site copy when configured |
| `bin/alert.sh`, `bin/diskcheck.sh` | a failed backup or a full disk raises an alert (marker, login banner, optional notifier) |
| `sql/verify.sql` | the post-restore facts (counts and schema facts only) |
| `cloud-init.yaml` | first-boot baseline for a NEW host only; the Oracle instance already exists |

Tests: `scripts/tests/test_server_kit*.py` and `test_server_runbook.py` (CI runs them with
every other script test).

## Rulings already made

- **Two hosts** (2026-11-07): core on Oracle, voice on Azure later. The core host runs no
  voice and no LiveKit, and opens only 22, 80 and 443.
- **CPU torch** on Linux (D1): the api image carries no CUDA libraries and builds on arm64.
- **Build on the host** for go-live, Docker's data root on the block volume (D2); images
  built in CI and pulled from GHCR by digest are a follow-up.
- **A separate 100 GB block volume** for `/srv` (D3): data, backups, Docker, swap.
- **Off-site backups to Azure Blob** with an immutability policy, a write-only SAS and age
  encryption, provisioned by the Terraform session (D4). Until then the backup runs locally
  and says so.
- **unified-monitoring-agent masked, oracle-cloud-agent kept** (D5); rpcbind masked.
- **Voice reaches Postgres over WireGuard** through a `pglink` proxy with its own
  `persona_voice` role (D6): Phase B, task T13.
- **Per-tenant MCP runtime: OFF** (`PERSONA_ALLOW_PER_TENANT_MCP=0`). See "Seam: per-tenant
  MCP".
- **Initiative: OFF** until the A5-R-1 re-run is green and the owner says so. The api
  template pins `PERSONA_INITIATIVE_ENABLED=false` whatever `.env` says.
- **The domain is openpersonas.si** (owner, 2026-11-07), DNS at Namecheap. The web is on
  Vercel at `openpersonas.si`, `app.` and `www.`; this host serves `api.openpersonas.si`
  and `connectors.openpersonas.si` (named once, in `hosts/oracle.env`); the voice host will
  serve `voice.` and `livekit.`. The old domains are dropped, with no alias: nothing was
  ever deployed on them. So **every provider registration moves** (see "New hostnames:
  what moves with them").
- **Stripe stays in test mode**, as production ran. Going live is a separate decision.

## Blockers before go-live

- **R9-285** (CRITICAL, path traversal in persona delete): its fix
  (`fix/persona-id-traversal`) must be merged to main, and this kit's branch rebased on it,
  before any deploy. No deploy without it.
- The **production Clerk** values (step 4).
- **DNS** for the two core hostnames (step 8) and the **ACME email**.
- Every **provider registration** moved to the new hostnames (see "New hostnames: what
  moves with them"), and the **web's** api and voice base URLs on Vercel.
- The seven **model chain** settings and the production **`MCP_CREDENTIAL_KEY`** (step 4).
- Not a blocker: the off-site backup target. Go-live may happen with it PENDING; the
  nightly backup says so every night until it exists.
- Voice blockers (R9-265 VAD threshold check, R9-216 latency sign-off) belong to Phase B.

## Owner inputs (checklist)

- [ ] SSH access to the instance as `ubuntu` from this PC (the coordinator has it).
- [ ] The instance's **public IPv4** (for `--public-ipv4` and DNS).
- [ ] DNS access to `openpersonas.si` (Namecheap).
- [ ] The `render_env.py --inventory` checklist (step 4) marked `[p]` or `[d]` per name, and
      the production value for every `[d]`. Look hardest at `PERSONA_API_JWT_PUBLIC_KEY` and
      `PERSONA_CONNECTORS_JWT_*` (the production Clerk instance, not a
      `*.clerk.accounts.dev` key), `MCP_CREDENTIAL_KEY` (must be the exact production key or
      stored MCP credentials become unreadable), the Stripe test keys and webhook secret,
      every connector bot token (a dev process polling the production Telegram bot steals
      its updates), and the behaviour flags marked "confirm".
- [ ] The seven shared settings (`PERSONA_*_MODELS` and
      `PERSONA_OPENROUTER_SUBSCRIPTION_MODE`). GitHub cannot read a secret back, so either
      supply the Production environment's values or confirm `.env` holds them.
- [ ] The CORS origins are already set to the web's three (`https://openpersonas.si`,
      `https://app.openpersonas.si`, `https://www.openpersonas.si`); change them only if the
      web moves.
- [ ] An email address for Let's Encrypt notices (`ACME_EMAIL`).
- [ ] Later: the off-site backup command once the Terraform session has the Azure Blob
      container and its write-only SAS (see "Backups off the host").

## Step 0: close the subdomain takeover (DONE 2026-10-23)

Done by the owner on 2026-10-23: every record naming a deleted `*.fly.dev` app is gone.
Kept as the rule for later: **never point a record at a name someone else can claim** (a
`*.fly.dev` app name, an Azure `*.cloudapp.azure.com` label). Use A records to static
addresses.

## Step 1: the Oracle console (owner)

Already done on 2026-11-07:

- the instance (VM.Standard.A1.Flex, 2 OCPU, 12 GB, Ubuntu 24.04, 45 GB boot volume);
- the **block volume**: 100 GB, Balanced, attached **paravirtualized**, read/write, at
  `/dev/oracleoci/oraclevdb`, still unformatted (provision formats it);
- the instance metadata service set to **IMDSv2** only;
- the security list admits **TCP 22, 80 and 443**. Nothing else is needed: the core host
  runs no LiveKit. (Phase B adds UDP 51820 from the Azure host's address only.)

Still to decide or do:

- **Reserved public IP**: if the console offers one free in this tenancy, convert the
  ephemeral address to reserved, so the DNS records survive an instance replacement.
- **Idle reclamation**: Oracle may reclaim an Always Free instance that is idle for 7 days
  (CPU p95, network AND memory under 20 %). With the api's resident models and Postgres
  the memory stays well above that. Upgrading the tenancy to Pay As You Go with a budget
  alert is the extra safety; Always Free resources stay free after the upgrade.
- **Volume backups**: manual ones only, at the milestones below (at most five Always Free
  backups in total, boot and block together). Do NOT attach a Bronze, Silver or Gold
  policy: they keep more than five. A boot volume backup includes the owner's other work.

## Step 2: put the code on the host (as ubuntu)

The repository is public, so the host needs no GitHub credential:

```bash
# on the host, as ubuntu
sudo git clone https://github.com/<owner>/<repo>.git /opt/open-persona/repo
sudo git -C /opt/open-persona/repo checkout main     # the branch carrying this kit
```

`provision.sh` hands the checkout to `deploy` (step 3).

## Step 3: provision (as ubuntu, with sudo)

```bash
# on the host, as ubuntu
sudo /opt/open-persona/repo/deploy/server/bin/provision.sh --profile oracle \
     --public-ipv4 <the instance's public IPv4>
```

From `hosts/oracle.env` it:

- creates `deploy` (uid 2000) with ubuntu's SSH key, writes the SSH lockdown (no root
  login, keys only, `AllowUsers deploy ubuntu`; it refuses to run if the allow list leaves
  out the user running it), fail2ban, unattended upgrades;
- formats the block volume ext4 (label `op-data`) **only if blkid finds no signature at
  all** (any blkid error refuses), mounts it by UUID at `/srv` with `nofail` (an existing
  mount there is accepted only if it is this device with the op-data label), and puts Docker's data root
  (`/srv/docker`), a 4 GB swapfile and all data under it (see "The block volume");
- hands Oracle's image firewall to ufw: a copy of the image's rules goes to
  `/root/open-persona-firewall-backup`, the `InstanceServices` chain (it keeps non-root
  processes off the iSCSI and metadata endpoints) is carried verbatim into
  `/etc/ufw/before.rules`, `netfilter-persistent` is masked, the image's live REJECT rules
  are removed, and ufw allows 22 (limited), 80 and 443 only;
- masks rpcbind and the unified-monitoring-agent (oracle-cloud-agent stays);
- installs Docker for arm64 (signing key fingerprint checked), the backup, disk-check and
  alert units, and records the profile and the public address in `/etc/open-persona/`.

The ubuntu session that ran it stays open: **before closing it, check from a second
terminal** that `ssh deploy@<ipv4>` works and `sudo -n true` succeeds, and that
`ssh ubuntu@<ipv4>` still works. From then on the kit runs as `deploy`; a re-run is the
same command with `sudo`.

**Reboot once now** (`sudo reboot`), then as deploy:

```bash
# on the host, as deploy
cd /opt/open-persona/repo/deploy/server
bin/preflight.sh          # the host section must pass; env and database checks fail until steps 4 to 6
```

It checks the architecture, the mounted block volume, Docker's data root, the data owner
(uid 2001), that Oracle's INPUT REJECT is gone and the `InstanceServices` chain is live
after the reboot, that no masked unit runs, swap and the clock. Once the api image exists
(step 6) it also starts a throwaway container that must resolve a public name (Docker
forwards DNS to Oracle's resolver, 169.254.169.254, which the metadata block lets through)
and must NOT reach the metadata service on 169.254.169.254:80. Then take a manual volume
backup of the block volume, "provisioned, empty".

## The block volume

`/srv` is the 100 GB block volume (`/dev/oracleoci/oraclevdb`). If it is ever missing
(detached, or the mount failed at boot), `nofail` lets the host boot, and then:

- Docker does not start (`RequiresMountsFor=/srv`), so no container comes up on an empty
  data root on the boot disk;
- `bin/deploy.sh`, `bin/backup.sh` and `bin/restore.sh` stop with "the block volume is
  missing".

Reattach it in the console (paravirtualized), then `sudo mount /srv` and
`sudo systemctl start docker`. `provision.sh` refuses to format a device that already holds
anything but its own `op-data` filesystem, and refuses to mount over a non-empty `/srv`.

## Step 4: the env files

On the **owner's PC**, from the repo. First the inventory (it writes nothing and shows
presence only, never a value), then mark each line `[p]` or `[d]`:

```bash
# on the PC, in the repo
uv run python -I deploy/server/render_env.py --dotenv .env --inventory > ~/op-inventory.txt
```

Then render, into a directory OUTSIDE the repo. The default `--host core` renders only the
files the core host reads, so no voice or LiveKit secret is written for it:

```bash
# on the PC, in the repo
uv run python -I deploy/server/render_env.py --dotenv .env --out ~/op-env
# Add --chains-from-dotenv only after confirming .env holds the production chains.
```

Edit the files in `~/op-env`:

- `__OWNER__`: fill in (the ACME email).
- `__GITHUB_PRODUCTION__`: the Production environment's value.
- `__MISSING_FROM_DOTENV__`: supply the value, or **delete the line** if production never
  set it (the code default then applies, exactly as on Fly).
- Every name marked `[d]`: replace the copied dev value with production's.
- `__OWNER_CLERK_PROD__`: every Clerk-derived value, from the **production** instance,
  whose Frontend API becomes `clerk.openpersonas.si`. **Change the domain of the EXISTING production Clerk instance in the Clerk dashboard;
  never create a new instance, or the restored users' ids stop matching.** A new instance issues new user
  ids, the api takes each user's identity from the token's `sub`, and every restored user
  would sign in as a stranger with their data unreachable. Then:
  - `PERSONA_API_JWT_PUBLIC_KEY` and `PERSONA_CONNECTORS_JWT_PUBLIC_KEY`: Configure > API
    keys > the JWT public key, PEM format. The same PEM in both.
  - `PERSONA_CONNECTORS_JWT_JWKS_URL`: `https://clerk.openpersonas.si/.well-known/jwks.json`.
  - `PERSONA_CONNECTORS_JWT_SECRET`: the HS256 development path; delete the line.
  - Check under Configure > JWT templates that the `persona-api` template has
    `aud` = `persona-api`.
- `PERSONA_VOICE_SERVICE_URL` stays **empty**: voice is not up yet, and the api answers its
  voice routes with a clean 503 until Phase B sets it.
- `offsite.env` stays empty until the off-site target exists.

Copy them to the host over SSH only (never a chat, mail or shared drive), and finish them
there:

```bash
# on the PC
scp ~/op-env/*.env deploy@<ipv4>:/opt/open-persona/env/
ssh deploy@<ipv4>
```

```bash
# on the host, as deploy
cd /opt/open-persona/repo/deploy/server
bin/fill_generated.sh          # new passwords for the three database roles
bin/preflight.sh --env-only    # must pass: roles separated, no dev Clerk instance
```

`/opt/open-persona/env/.generated` now holds the only copy of the new database passwords.
Put the whole `/opt/open-persona/env` directory in the owner's password manager. Delete
`~/op-env` from the PC afterwards.

## Step 5: restore

**R9-285 must be merged first** (see "Blockers"). On the owner's PC, checksum a COPY of the
set (never write into the original directory), then upload:

```bash
# on the PC
mkdir -p ~/op-restore && cp ~/Backups/fly-2026-10-02/{persona.dump,globals.sql,persona_volume.tgz} ~/op-restore/
(cd ~/op-restore && sha256sum persona.dump globals.sql persona_volume.tgz > SHA256SUMS)
scp ~/op-restore/* deploy@<ipv4>:/srv/open-persona/restore/
rm -rf ~/op-restore
```

On the host:

```bash
# on the host, as deploy
cd /opt/open-persona/repo/deploy/server
bin/restore.sh --backup-dir /srv/open-persona/restore
```

The dump is the custom format, which does not depend on the architecture: an x86 dump
restores on arm64. What a correct restore prints (from the 2026-10-23 rehearsal against
this exact set):

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

One fact was added after that rehearsal, so it is not in the output above:
`personas_unexpected_ids|0`, the count of personas whose id is not in the minted format
(R9-285). It must be 0: the api refuses any other id in a URL, so such a persona could never
be opened or deleted. A higher count prints a warning and the restore carries on; stop there
and bring the count to the owner before deploying.

`memory_orphan_owners` is also newer than that rehearsal: how many groups of memory rows belong
to a persona or conversation that no longer exists (R9-284). A count above 0 prints a warning and
the restore carries on. It is expected on any dump older than migration 062, and step 5 settles
it.

Apart from those warnings and the memory chains one below, anything else stops the script.
`pg_restore` runs in one transaction, so a failed restore
leaves no tables behind, and the role and extension steps are idempotent: fix the cause and
run `bin/restore.sh` again. If the failure came AFTER `pg_restore` (a verification check),
the tables are there and the script refuses to restore over them; start clean with
`bin/compose.sh down` then `docker volume rm open-persona_pgdata`. **Only before
go-live:** once real users write to this database, that command destroys production.

The volume is unpacked as uid 2001 (the data uid), never as root, after a scan that refuses
any symlink, hardlink, device node, absolute path or `..` path; no owners or modes come from
the archive. `bin/restore.sh --roles-only` re-applies the roles and grants to an existing
database (a password rotation), touching no data.

### Check the memory chains

The first deploy runs migration 059, which builds the index that allows one current version
per memory chain (R9-205). It cannot be built while any chain has two. `restore.sh` printed
the count as `memory_chains_two_heads`; this is the same count by kind:

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
bin/compose.sh exec -T postgres psql -U persona -d persona -X -q -t -A -F '|' <<'SQL'
SELECT kind, count(*) FROM (
  SELECT kind FROM memory_chunks
   WHERE logical_id IS NOT NULL AND superseded_by IS NULL
   GROUP BY persona_id, kind, logical_id HAVING count(*) > 1) AS two_heads
 GROUP BY kind ORDER BY kind;
SQL
```

- **No rows** is the expected answer. Carry on to step 6.
- **Any rows**: nothing is lost, both versions are on disk, and the migration will refuse,
  changing nothing, until this is settled. See what is there, as the migrate service (it
  holds the superuser connection):

  ```bash
  # on the host, as deploy, in /opt/open-persona/repo/deploy/server
  bin/compose.sh run --rm migrate sh -c 'persona repair --all-personas --database-url "$DATABASE_URL"'
  ```

  That changes nothing and prints each chain and what can be done with it. Settle the
  chains two saves raced by adding `--resolve-raced --apply` to the same command: every
  version is kept, the later one stays current, and each chain gets an audit row. A chain
  it reports as "left alone" needs a person: stop and bring the output to the owner.

### Sweep the memory summaries

Forgetting something now also deletes every memory summary written from it. The dump
predates that, so it can hold summaries of evidence a person had already asked to forget
(R9-209). This looks for them, with no model and no cost:

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
bin/compose.sh run --rm migrate sh -c 'persona repair --all-personas --database-url "$DATABASE_URL" --forgotten-evidence'
```

- **"No memory summary ... cites anything forgotten"**, exit 0: nothing to do.
- **A count per persona**, exit 1: add `--apply` to delete those versions (each deletion is
  audited with ids and counts only), then run it again to confirm. Those personas may
  briefly know less until the next consolidation pass. That is expected.

### Sweep what deleted personas left behind

Deleting a persona used to delete only its row, so a dump can still hold the memory of personas
the person had deleted, and of the conversations that went with them (R9-284). This is the
`memory_orphan_owners` count above. The same command also finds their initiative notices and the
daily scan schedules still firing for them. It uses no model and costs nothing:

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
bin/compose.sh run --rm migrate sh -c 'persona repair --all-personas --database-url "$DATABASE_URL" --orphaned'
```

It changes nothing without `--apply`, and prints ids and counts, never content.

- **"Nothing here was left behind"**, exit 0: nothing to do.
- **A count per persona or conversation**, exit 1: show the owner the list. These rows cannot be
  put back, so `--apply` waits for the owner's yes. Then add `--apply` (each removal is audited
  in `audit_log` with ids and counts only) and run it once more without `--apply` to confirm it
  says nothing is left.

## Step 6: deploy

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
bin/deploy.sh
```

The first build runs on the host's 2 cores: expect 10 to 20 minutes (the image carries CPU
torch, no CUDA). It competes with nothing yet; later deploys run off-hours. The migrate step
takes the restored dump from `058` to head (`062` today: `059` the memory chain index, `060`
the channel's persona pointer, `061` the erasure ledger, `062` the memory owner guard). The first
boot downloads the HF models (the bge
embedder, the reranker and the crisis encoder, about 1 to 2 GB) into the data volume and
loads them in 1 to 2 minutes on Ampere, hence the long health timeouts. `deploy.sh` ends with
`api /livez: ok` and `api /healthz (database): ok`. The Caddy smoke test warns until DNS
points here.

Before DNS, check through an SSH tunnel from the PC:

```bash
# on the PC: a tunnel, then the api through it (from a second PC terminal)
ssh -L 8000:127.0.0.1:8000 deploy@<ipv4>
curl -fsS http://127.0.0.1:8000/healthz
```

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
bin/compose.sh logs api | grep -iE "model chain|mcp runtime|connectors"
```

The api boot log should show the model chains, the MCP startup summary with the per-tenant
runtime `off (reason: unconfigured)`, and the connector transports starting.

## Step 7: volume backup

A manual backup of the block volume: "restored, not yet public". This is the move's
rollback point.

## Step 8: DNS and certificates

At Namecheap (Advanced DNS for openpersonas.si), create A records, TTL 5 min, for the
core host's two names (the names `hosts/oracle.env` holds):

```
api.openpersonas.si           A  <ipv4>
connectors.openpersonas.si    A  <ipv4>
```

AAAA records only if the VCN and the instance have IPv6 configured. **No record for the
voice and livekit names yet**: they will point at the voice host (Phase B), and the core
host's Caddy does not serve them.

Then on the host: `bin/preflight.sh` (the two names should resolve to the recorded public
address) and watch Caddy obtain STAGING certificates: `bin/compose.sh logs -f caddy`. When
every name has one, switch to production certificates:

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server (the curl from anywhere)
sed -i 's|^ACME_CA=.*|ACME_CA=https://acme-v02.api.letsencrypt.org/directory|' /opt/open-persona/env/caddy.env
bin/compose.sh up -d --force-recreate caddy
curl -fsS https://api.openpersonas.si/healthz      # from anywhere
```

Once stable for a day, raise the TTLs to 3600.

## Step 9: verify the product

- Sign in on the web app; open a conversation (history from the restore is there); send a
  message and watch it stream (SSE through Caddy).
- An uploaded image or document from before the move opens (the workspace volume).
- Telegram (and any other linked channel): a message round trip.
- Stripe dashboard (test mode): send a test event to
  `https://api.openpersonas.si/v1/billing/webhook`; it returns 200.
- MCP: an existing GitHub OAuth connection still works (proves `MCP_CREDENTIAL_KEY`).
- Voice is expected to be unavailable: the api's voice routes answer 503 until Phase B.
- `bin/preflight.sh` passes in full.

## Step 10: the operator passes that need no voice

The **Stripe test-mode pass** for the auto top-up invoice chain (R9-177): a card crossing the
$2 line, a retry within the hour, a 3DS card, a decline card. The V6 voice pass and the
R9-216 latency sign-off move to Phase B.

## Step 11: sign-off

- Delete `/srv/open-persona/restore` on the host (production data, no longer needed there).
- Delete the "restored, not yet public" volume backup from step 7 once users write: it
  holds the database and secrets as they were before go-live.
- Optional: CAA records (`0 issue "letsencrypt.org"`) on both domains.
- Confirm the nightly backup ran (`systemctl list-timers open-persona-backup.timer`,
  `ls /srv/open-persona/backups/nightly`, `journalctl -u open-persona-backup`). Its log says
  `off-site copy PENDING` until the off-site target exists.
- A manual volume backup, "live".
- Initiative stays OFF until the A5-R-1 re-run.

## New hostnames: what moves with them

The production hostnames changed with the domain (2026-11-07), so every place outside this
host that names them must be updated before go-live, by the owner, in each provider's
dashboard. And the values copied from `.env` in step 4 may still carry an old domain:
check each of these in the rendered files.

| where | the new value |
|---|---|
| Stripe (test mode): webhook endpoint | `https://api.openpersonas.si/v1/billing/webhook` (then the new signing secret into `PERSONA_STRIPE_WEBHOOK_SECRET`) |
| Stripe checkout and portal return URLs (`PERSONA_STRIPE_CHECKOUT_*_URL`, `PERSONA_STRIPE_PORTAL_RETURN_URL`) | on `https://app.openpersonas.si` |
| Discord and Slack OAuth redirect URIs (their developer portals, and `PERSONA_CONNECTORS_*_OAUTH_REDIRECT_URI`) | `https://connectors.openpersonas.si/discord/oauth/callback`, `.../slack/oauth/callback` |
| Slack events URL | `https://connectors.openpersonas.si/slack/events` |
| Telegram webhook | `https://connectors.openpersonas.si/telegram/webhook` |
| Twilio SMS and WhatsApp webhooks and status callbacks | `https://connectors.openpersonas.si/{sms,whatsapp}/{webhook,status}` |
| Postmark inbound webhook | `https://connectors.openpersonas.si/email/webhook` |
| GitHub OAuth app for MCP (`PERSONA_MCP_OAUTH_REDIRECT_BASE_URL`) | on `https://api.openpersonas.si` |
| Clerk production instance | its domain `openpersonas.si` (Frontend API `clerk.openpersonas.si`), the `persona-api` JWT template. Change the domain of the EXISTING production Clerk instance in the Clerk dashboard; never create a new instance, or the restored users' ids stop matching. |
| Vercel (the web's env) | the api base URL `https://api.openpersonas.si`; the voice base URL `https://voice.openpersonas.si` (Phase B) |

## Seam: per-tenant MCP

Per-tenant MCP is OFF at launch. The coming spec uses **provider-hosted MCP**: the model
providers call remote MCP servers themselves, so nothing new needs to run on this host and
no container runtime is planned. The seam is marked `SEAM: per-tenant MCP runtime` in
`docker-compose.yml`, and whatever mechanism comes is held to this, nothing more:

- It reaches the api only through settings in `api.env` (`PERSONA_ALLOW_PER_TENANT_MCP`
  plus its own), added to the templates, `render_env.py` and preflight in the same change.
- **The api never gets the Docker socket**, `/run` or `/var/run` mounts, `privileged` or
  `DOCKER_HOST`. The api holds every tenant's data and credentials. The kit's tests fail on
  any of these, for every service.
- If it ever needs an endpoint WE host for providers to call, that is a new public route
  through Caddy with its own auth and request limits, and a firewall review. None is
  planned.

## Alerts

A failed nightly backup, a disk-usage check over 85 % (daily, `bin/diskcheck.sh`) or a
backup refused for lack of free space (`OP_BACKUP_MIN_FREE_GB` in the profile, 20 on
Oracle) is never silent: the unit's `OnFailure=` runs `bin/alert.sh`, which

1. appends a line to `/var/lib/open-persona/alerts/ALERTS`, shown on every SSH login (the
   banner) and failed by `bin/preflight.sh` until acknowledged with `bin/alert.sh --ack`;
2. runs `OP_ALERT_CMD` from `alert.env` if the owner set one (a webhook or a mail command;
   examples in `env/alert.env.example`). The marker never depends on it.

## Backups off the host

A backup on the host's own disk does not survive the disk, and Oracle's volume backups live
in the same account. Ruling D4: **Azure Blob**, provisioned by the Terraform session:

- a container with a **time-based immutability policy** (14 to 30 days), so a compromised
  host cannot delete or overwrite history;
- a **write-only SAS** scoped to that container: the only credential this host holds;
- each night's directory **encrypted with `age`** to a public key before it leaves; the
  decryption key stays OFF the host (the owner's password manager and one offline copy).

Wiring: `/opt/open-persona/env/offsite.env` sets `OFFSITE_CMD`, a command that receives the
night's directory as its argument (for example a script under `/opt/open-persona/bin` that
encrypts and uploads). A failing copy fails the unit and raises an alert. Until it is set
the backup runs locally and logs `off-site copy PENDING` every night. After setup, do one
test restore into a throwaway database (`bin/restore.sh --container ... --db-only`).

## Rolling back

**The move:** there is no Fly to go back to. The rollback is the "restored, not yet public"
volume backup from step 7 plus the untouched backup set on the PC.

**A deploy:** `deploy.sh` tags the api image it replaces as `:previous` and dumps the
database before migrating (`/srv/open-persona/backups/pre-deploy-*.dump`):

```bash
# on the host, as deploy, in /opt/open-persona/repo/deploy/server
docker image tag open-persona/api:previous open-persona/api:current
bin/compose.sh up -d
```

If that deploy ran a migration the old code cannot live with, restore the pre-deploy dump
into an emptied database with `pg_restore` (the same commands `bin/restore.sh` uses) before
starting the old image.

**Losing the host:** a new instance, the block volume reattached (or a new one), steps 2 to
6 with the newest off-site nightly backup as `--backup-dir`. A nightly backup carries
`facts.txt`, so `restore.sh` checks the schema shape against that night's own facts.

## Routine operations

| on Fly | here (as deploy, from `/opt/open-persona/repo/deploy/server`) |
|---|---|
| `flyctl ssh console -a open-persona-api` | `bin/compose.sh exec api bash` |
| `flyctl ssh console -C "printenv NAME"` | `bin/compose.sh exec api printenv NAME` (prints a secret: never paste it) |
| `flyctl logs -a open-persona-api` | `bin/compose.sh logs -f api` |
| `flyctl status` | `bin/compose.sh ps` |
| release_command | `bin/compose.sh run --rm migrate` |
| `flyctl secrets set` | edit `/opt/open-persona/env/<service>.env`, then `bin/compose.sh up -d <service>` |
| `fly scale count` | not applicable: one api, by design |

- **Updates:** security updates install by themselves; the host never reboots by itself.
  When `/var/run/reboot-required` exists, reboot in a quiet hour (`sudo reboot`; every
  service has `restart: unless-stopped`, and Docker waits for `/srv`).
- **Disk:** `docker system df`; old images: `docker image prune` (keeps `:current` and
  `:previous`); build cache: `docker builder prune` after a build.
- **A new release:** `git -C /opt/open-persona/repo pull`, then `bin/deploy.sh`.

## Sizing and the host profile

`hosts/oracle.env` holds every size: the memory budget (12 GB with 3.5 GB kept free of
container limits), postgres (3 GB limit, never swapping, 1 GB shared buffers, 60
connections), the api (4 GB: the resident models take about 2 to 2.5 GB) and Caddy. The
owner's other work is assumed idle (2026-11-07). If it starts using real memory or CPU,
raise `OP_HOST_RESERVED_GB` and lower the limits until the kit's budget test passes, then
`bin/deploy.sh --skip-build`. The api stays one uvicorn process, always.

Expect chat latency to be the model provider's plus about 0.2 to 0.4 s of local inference
(embedding, the crisis check and reranking) on the two Ampere cores. If that hurts,
`PERSONA_RECALL_RERANK_ENABLED` is the first lever.

## Phase B: voice on Azure

Voice and LiveKit come up on the Azure host (the Terraform session provisions it; task T13
gives this kit its voice profile). On the core host the only change is one env line:

- `PERSONA_VOICE_SERVICE_URL=https://voice.openpersonas.si` in `api.env`, then
  `bin/compose.sh up -d api`. The api forwards the caller's Clerk token to voice; there is
  no service credential between them.

What T13 builds (ruling D6), so that nothing else on Oracle changes by hand:

- Voice reads and writes Postgres directly (D-V1-4). It reaches it over a **WireGuard**
  tunnel (UDP 51820 open only to the Azure host's address, in the OCI security list and in
  ufw), through a `pglink` proxy published only on the tunnel address, so postgres stays on
  its internal network. Voice logs in as its own `persona_voice` role (a member of
  `persona_app`, RLS enforced), so its credential rotates on its own.
- The Azure host should be in **germanywestcentral** (Frankfurt), next to eu-frankfurt-1:
  a voice turn makes several database round trips.
- DNS: `voice.openpersonas.si` and `livekit.openpersonas.si` as A records to the Azure
  host's static public IP (never a CNAME to a claimable Azure name). The voice host opens
  80/443 and LiveKit's 7881/tcp and 7882/udp; `Caddyfile.voice` serves both names.
- Before the first voice deploy: the R9-265 VAD threshold check. Voice is done when the
  R9-216 call meets the 800 ms P50 budget.

## Later: CI/CD and images

`.github/workflows/deploy.yml` still targets Fly and is manual-only. `images.yml` already
builds and smoke-tests the api image for amd64 and arm64 on every relevant pull request,
pushing nothing. The follow-up after go-live (ruling D2):

- **Images:** the repository is public, so GHCR storage and pulls of public images cost
  nothing. Build once in Actions, push by digest, and `bin/compose.sh pull` on the host
  instead of building on its two cores.
- **Deploys:** `workflow_dispatch` from `main` only, `environment: production`, the same
  fork and branch gates `scripts/check_deploy_wiring.py` enforces. Job 1 is the existing
  `validate-model-chains`. Job 2 holds three Production secrets, `ORACLE_SSH_KEY` (a
  dedicated ed25519 key), `ORACLE_KNOWN_HOSTS` (the host's pinned key) and `ORACLE_HOST`, and
  runs `ssh deploy@$ORACLE_HOST <commit sha>` with the seven settings on stdin. On the host
  that key's line is `command="/opt/open-persona/bin/ci-deploy",restrict ssh-ed25519 ...`:
  the key can run exactly one script, which validates the SHA, checks it out, writes the
  seven settings (each checked against a strict per-name pattern) and runs `deploy.sh`.
