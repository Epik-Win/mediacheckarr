# mediacheckarr
Checkrr library integrity scans combined with import-triggered mediacheck compatibility and identity checks.

This fork runs both workers in one Docker container. Mediacheck only processes
Sonarr/Radarr import and upgrade events; full Checkrr scans are manual by
default. See [the combined Docker setup](docker/README.md) for deployment.

[![Docker Pulls](https://img.shields.io/docker/pulls/epik-win/mediacheckarr)](https://hub.docker.com/r/epik-win/mediacheckarr) ![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/Epik-Win/mediacheckarr/total) ![GitHub License](https://img.shields.io/github/license/Epik-Win/mediacheckarr) [![GitHub Release](https://img.shields.io/github/v/release/Epik-Win/mediacheckarr)](https://github.com/Epik-Win/mediacheckarr/releases) ![GitHub Release Date](https://img.shields.io/github/release-date/Epik-Win/mediacheckarr)


## Features

Checkrr validates media files with ffprobe, container and MIME checks, and
optional deeper ffmpeg checks. It stores successful file hashes so unchanged
files can be checked efficiently on later runs. When configured, Checkrr can
ask Sonarr or Radarr to replace files that fail integrity checks.

The combined Docker image also runs mediacheck for Sonarr/Radarr import
events. Import-triggered checks and manual full-library Checkrr scans are
separate workflows.

## Screenshots
![Idle screenshot](./screenshots/Idle.png?raw=true)
![Running screenshot](./screenshots/Running.png?raw=true)

## Installation and running checkrr
### cli (without package manager)
* Install prerequisite packages via your package manager or by downloading the installer (for windows): ffmpeg
* Make sure ffprobe is in your $PATH var. If you installed from a Linux/macOS package manager, it is. If you are on windows, you'll need to make sure you can run ffprobe from a basic command prompt/powershell.
* Grab a release from the releases page.
* Copy the example config from the repo: `wget https://raw.githubusercontent.com/Epik-Win/mediacheckarr/main/checkrr.yaml.example -O checkrr.yaml`
* Edit the config in your favorite editor. Make sure you remove any sections you aren't using. (If you aren't using influxdb 1 and/or 2 for example, you should remove the entire stats block from your config.)If you aren't sure what the minimal config file can look like, check https://raw.githubusercontent.com/Epik-Win/mediacheckarr/main/checkrr.yaml.minimal.
* To run checkrr as a daemon, use `checkrr -c /path/to/checkrr.yaml`. If you'd like checkrr to run once and then exit (useful for running in your own cron daemon) `checkrr -c /path/to/checkrr.yaml --run-once`.

### docker
Use the integrated Docker image and setup guide in [docker/README.md](docker/README.md).


### Running Checkrr
cli as a daemon:
``` checkrr -c /etc/checkrr.yaml ```

cli as a one-off:
``` checkrr -c /etc/checkrr.yaml -o```

docker:
``` docker compose -f compose.yaml up -d --build ```

The Checkrr web UI's **Run Now** action starts a full library scan; it is not
scheduled unless `checkrr.cron` is configured.

## Upgrading to 3.1 or newer
checkrr > 3.1 has changed the way arr services are handled. Please review the example config and bring your config into compliance prior to running checkrr. With the 3.1 release checkrr supports having multiple of each arr service. So you could have 3 sonarr instances connected. Each arr config under `arr:` has a `service` key to tell checkrr what service type it is. This can be set to `sonarr`, `radarr`, or `lidarr`. Please note that if you are running on docker, you will likely want to setup path mappings for each service. checkrr will attempt to translate the paths that the arr services see when working with their APIs.

## Upgrading to 3.5 or newer
checkrr > 3.5 has changed the way logging is handled. Please review the example config and bring your config into compliance prior to running checkrr. Generally you can get away with not including a logging section and you will only get a nagging warning about using the default fallback logger. You *do* need to specify a language, as of the time of writing, only en-us is supported, but anyone is free to provide good translations if you happen to be a native or professional speaker. The language option is in the example config.

## Building
See [CONTRIBUTING.md](CONTRIBUTING.md)

## Contributions
See [CONTRIBUTING.md](CONTRIBUTING.md)

## Security
If you think you have found a security issue with checkrr, see [SECURITY.md](SECURITY.md) for reporting instructions.

## FAQ
### How do I add multiple paths for checkrr to check?
You can specify multiple folders to check via the config file
```yaml
checkrr:
  checkpath:
    - /media/TV_Shows
    - /media/Movies
```
