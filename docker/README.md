# Combined Docker setup

This image runs Checkrr and mediacheck in one container:

- Checkrr's deep library scan and web UI remain available. Automatic scans
  are disabled when `checkrr.cron` is empty; run a scan from the UI or API.
- Mediacheck polls only the Sonarr and Radarr import queues, then performs
  the configured probe, targeted fixes, and import identity check. Deferred
  checks are retried. It does not scan the library.

## Configure

1. Copy `.env.example` to `.env` and set host paths, Docker network, and UID/GID.
   Keep `.env` private.
2. Create the config and persistent-data directories. Copy
   `docker/checkrr.yaml.example` to `<config>/checkrr.yaml`, replace the Arr
   API-key placeholders, and copy `mediacheck/mediacheck.yaml.example` to
   `<config>/mediacheck.yaml`.
3. Ensure Sonarr and Radarr each have a writable `mediacheck-queue` directory
   under their config directory. The combined container mounts only those
   queue directories, not the Arr application configs.
4. In Sonarr and Radarr, add a Custom Script connection for both **On Import**
   and **On Upgrade**, using `mediacheck/arr_hook.sh`. The script must be
   installed at `/config/mediacheck-hook.sh` inside each Arr container.
5. Configure the external Docker network so the container can reach Sonarr,
   Radarr, Bazarr, LiteLLM, and Debuggarr by the hostnames in the example
   config. The secret files are mounted read-only and are never stored in this
   repository.
6. Build and start:

   ```sh
   docker compose -f compose.yaml up -d --build
   ```

Checkrr's UI is bound to `127.0.0.1:8585` by default. Check container health
and logs with `docker compose -f compose.yaml ps` and
`docker compose -f compose.yaml logs -f`.

The Arr API configuration is shared through `checkrr.yaml`; configure
`address`, `port`, `baseurl`, and API keys for both services. Its address must
be resolvable from the configured Docker network. Set `checkrr.cron` to a
non-empty cron expression only if automatic full-library scans are wanted.

## Updating the Arr hook

The hook file is a template/script that must be copied into both Arr config
directories and made executable there. It writes one small job per imported
video to the shared queue directory. The combined container claims jobs across
bind mounts, so the queue directories must be writable by the configured
container UID/GID.

The persistent data mount stores Checkrr's database, mediacheck state, logs,
and deferred jobs. Quarantined media is stored separately at `/quarantine`.
