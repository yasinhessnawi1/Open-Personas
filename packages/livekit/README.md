# `open-persona-livekit`, the self-hosted LiveKit SFU on Fly.io

Runs the LiveKit OSS media server as a **separate Fly app** so prod voice stops
depending on LiveKit Cloud. Audio only. It is the **same** server image
(`livekit/livekit-server:v1.13.1`) the community edition already runs locally via
`docker-compose.yml` and `livekit.dev.yaml`: one config story, two deploy targets.

> **Status: deployed.** CI ships this app on every release through the
> `deploy-livekit` job, and prod voice runs on it instead of LiveKit Cloud. The
> app and its dedicated IPv4 are provisioned out of band; the workflow only
> ships the image. Read the runbook before changing the config, because WebRTC
> on Fly depends on the IPv4 and port setup as much as on the server config.

## Why a separate app

The voice VM (`open-persona-voice`) runs the embedder, VAD, and STT, TTS, and LLM
work in process. Co-locating the media SFU would re-create the CPU and event loop
starvation the voice VM scale-up just fixed. The SFU gets its own machine.

## Files

| File | Purpose |
|---|---|
| `fly.toml` | Fly app config: TCP signaling (443 to 7880), ICE over TCP (7881), **UDP single port mux (7882, external equals internal)**, `performance-1x` VM. |
| `livekit.yaml` | Prod LiveKit config, derived and hardened from `livekit.dev.yaml`. Single port UDP, `use_external_ip:false`, TURN present but commented. |
| `entrypoint.sh` | Resolves the dedicated Fly v4 into `--node-ip`, binds to `fly-global-services` (the Fly UDP rule), and asserts `LIVEKIT_KEYS`. |
| `Dockerfile` | Thin layer over `livekit/livekit-server:v1.13.1` plus the config and entrypoint. |

## The two hard Fly facts (why the config looks the way it does)

1. **UDP needs a dedicated IPv4.** `fly ips allocate-v4` is mandatory. A shared
   anycast v4 won't carry UDP, and IPv6 UDP is unsupported.
2. **Fly does NOT rewrite the UDP port.** External port must equal internal port,
   so the UDP mux is `7882` on both sides and equals `rtc.udp_port`.

Full reasoning and citations: `docs/research/livekit_selfhost_fly.md`.
Operator deploy and browser pass steps: that research doc's §4, plus the runbook
(`docs/research/livekit_selfhost_fly_runbook.md`).

## Re-pointing voice (reversible, no code change)

Voice reads `PERSONA_VOICE_LIVEKIT_URL`, `_API_KEY`, and `_API_SECRET` from env.
Switch to self-hosted by setting those secrets on `open-persona-voice`
(`PERSONA_VOICE_LIVEKIT_URL=wss://open-persona-livekit.fly.dev`); revert by
setting them back to the LiveKit Cloud values. One URL swap each way.

## Local and community

Nothing changes. Community already runs this server via
`docker compose up -d livekit` (`livekit.dev.yaml`). This package is the prod
deploy target only.
