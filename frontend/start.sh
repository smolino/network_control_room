#!/bin/sh
# Reload nginx periodically so it picks up certificates the `certbot`
# service renews in the background, without needing to restart the container.
set -e

(
    while true; do
        sleep 6h
        nginx -s reload 2>/dev/null || true
    done
) &

exec nginx -g "daemon off;"
