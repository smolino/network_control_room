#!/bin/sh
set -e

if [ "$DISABLE_SSL" = "true" ]; then
    # Dev mode: no Let's Encrypt certificate available locally, serve plain HTTP.
    cp /etc/nginx/templates/nginx.dev.conf /etc/nginx/conf.d/default.conf
else
    cp /etc/nginx/templates/nginx.ssl.conf /etc/nginx/conf.d/default.conf

    # Reload nginx periodically so it picks up certificates the `certbot`
    # service renews in the background, without needing to restart the container.
    (
        while true; do
            sleep 6h
            nginx -s reload 2>/dev/null || true
        done
    ) &
fi

exec nginx -g "daemon off;"
