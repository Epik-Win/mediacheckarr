#!/bin/sh
# Install as a Sonarr/Radarr Custom Script for On Import and On Upgrade.
# The combined mediacheckarr container polls the shared queue directories.

Q=/config/mediacheck-queue
ev="${sonarr_eventtype:-${radarr_eventtype:-}}"
path="${sonarr_episodefile_path:-${radarr_moviefile_path:-}}"

case "$ev" in
    Test)
        mkdir -p "$Q" && [ -w "$Q" ] || { echo "can't write to $Q" >&2; exit 1; }
        echo "mediacheck hook OK"; exit 0 ;;
    Download) ;;
    *) exit 0 ;;
esac

case "$path" in
    *.[mM][kK][vV]|*.[mM][pP]4|*.[mM]4[vV]|*.[mM][oO][vV]) ;;
    *.[aA][vV][iI]|*.[tT][sS]|*.[mM]2[tT][sS]|*.[wW][mM][vV]) ;;
    *) exit 0 ;;
esac

mkdir -p "$Q"
job="$(date +%s)-$$.job"
{
    echo "app=${sonarr_eventtype:+sonarr}${radarr_eventtype:+radarr}"
    echo "path=$path"
    echo "upgrade=${sonarr_isupgrade:-${radarr_isupgrade:-False}}"
    echo "download_id=${sonarr_download_id:-${radarr_download_id:-}}"
    echo "release=${sonarr_episodefile_scenename:-${radarr_moviefile_scenename:-}}"
} > "/config/.mediacheck-$job" && mv "/config/.mediacheck-$job" "$Q/$job"
