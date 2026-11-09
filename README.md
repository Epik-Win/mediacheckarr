# mediacheckarr

mediacheckarr combines Checkrr library-integrity scans and web UI with
Mediacheck's import-triggered media compatibility and identity checks in one
Docker image.

## What runs automatically

- Mediacheck processes Sonarr and Radarr **On Import** and **On Upgrade**
  events from the configured queue directories. It does not poll or scan the
  entire library. Each queued file gets a lightweight readability probe and
  narrow checks for known playback problems; ordinary imports are not
  automatically converted.
- The current targeted fixes are narrowly scoped: MP4-family files with badly
  interleaved tracks (the Tuner subtitle playback issue) are remuxed, and
  HEVC 10-bit MKVs with EAC3 5.1(side) audio receive an additional AAC
  compatibility track while keeping their original audio and subtitles. The
  fixes do not re-encode video or perform general-purpose transcoding.
- If `policy.reacquire_unreadable` is enabled (it is enabled in the example),
  an unreadable file can be quarantined and requested again through Sonarr or
  Radarr.
- Optional identity checking can flag a file whose audio/transcript does not
  match its expected movie or episode. The example config leaves this check
  disabled (`identity.enabled: false`); enabling it requires a working
  transcription endpoint and Bazarr reference configuration. Findings are
  sent privately to the configured admin through Debugarr and, optionally,
  Telegram for review. Mediacheck does not post to a Discord channel or
  automatically replace a file based on an identity mismatch.
- Checkrr full-library integrity scans are separate. The supplied Docker
  config leaves `checkrr.cron` empty, so a full scan must be started manually
  from the Checkrr UI or API. Setting a cron expression enables automatic
  library scans. Checkrr supports Telegram notifications independently of
  Mediacheck; configure its `notifications.telegram` section in
  `checkrr.yaml`.

## Docker setup

Docker Compose is the recommended combined deployment. Follow the detailed
[combined Docker setup guide](docker/README.md). In brief:

1. Copy `.env.example` to `.env`. Set the host paths, Docker network, and UID
   and GID for your deployment.
2. Create the config and persistent-data directories. Copy
   [`docker/checkrr.yaml.example`](docker/checkrr.yaml.example) to
   `<config>/checkrr.yaml`, then set the Sonarr and Radarr API keys, service
   addresses, media paths, and path mappings.
3. Copy
   [`mediacheck/mediacheck.yaml.example`](mediacheck/mediacheck.yaml.example)
   to `<config>/mediacheck.yaml`. Set media paths and policies, and review the
   optional identity-check settings before enabling them. The targeted
   compatibility fixes described above are built in.
4. Ensure Sonarr and Radarr each have a writable `mediacheck-queue` directory
   under their config directory. Install `mediacheck/arr_hook.sh` inside each
   Arr container as `/config/mediacheck-hook.sh`; configure it for both
   **On Import** and **On Upgrade** custom-script events.
5. Ensure the container can access the media paths and external Docker
   network, and mount the Bazarr, local-AI, and Debugarr secret files needed
   by the features you enable. For direct Mediacheck Telegram alerts, add
   `MEDIACHECK_TELEGRAM_BOT_TOKEN` and `MEDIACHECK_TELEGRAM_CHAT_ID` to the
   file mounted as `/run/secrets/debuggarr.env`. This sends alerts privately
   to that chat; it does not enable Telegram command handling.
6. Pull and start the published GitHub Container Registry image:

   ```sh
   docker compose -f compose.yaml pull
   docker compose -f compose.yaml up -d
   ```

Each push to `main` publishes a multi-architecture
`ghcr.io/epik-win/mediacheckarr:latest` image. Version tags and immutable
source-commit tags are published as well.

The Checkrr web UI is bound to `127.0.0.1:8585` on the host by default. Keep it
on a trusted interface; do not expose it directly to the public internet.
Check container health and logs with:

```sh
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs -f
```

The persistent data directory stores Checkrr's database, Mediacheck state,
logs, and deferred jobs. Quarantined media is stored separately at the
configured quarantine path. Back up these directories and review quarantine
contents before deleting or restoring them.

Debugarr can also provide Telegram commands for bug management and Mediacheck
identity-review decisions; see the Debugarr setup guide. Checkrr's Telegram
notifier sends configured event notifications only. Configure it under
`notifications.telegram` in `checkrr.yaml`:

```yaml
notifications:
  telegram:
    apitoken: "replace-with-a-dedicated-bot-token"
    username: "@your_telegram_username"
    chatid: 123456789
    notificationtypes:
      - reacquire
      - unknowndetected
      - startrun
      - endrun
```

Checkrr requires a numeric chat ID and keeps its bot token in `checkrr.yaml`;
protect that runtime config and never commit real credentials. Use a separate
bot token from Debugarr/Onboardarr because Checkrr also reads Telegram updates
at startup.

## Standalone Checkrr CLI

The image includes Checkrr. For installation outside Docker, install a
Checkrr release along with ffmpeg and ffprobe, then use the configuration and
run commands documented in
[`checkrr.yaml.example`](checkrr.yaml.example) and
[`checkrr.yaml.minimal`](checkrr.yaml.minimal). A one-off CLI scan can be run
with:

```sh
checkrr -c /path/to/checkrr.yaml --run-once
```

## Project links

- [Checkrr configuration example](checkrr.yaml.example)
- [Mediacheck configuration example](mediacheck/mediacheck.yaml.example)
- [Contributing](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)

## License

MIT. See [LICENSE](LICENSE).
