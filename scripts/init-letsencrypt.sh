#!/usr/bin/env bash
# Bootstraps the Let's Encrypt certificate used by frontend/nginx.conf.
#
# Run this once before `docker compose up`, from the repo root:
#
#   ./scripts/init-letsencrypt.sh
#
# It brings up `backend` + `frontend` itself (nginx needs to be running to
# answer the ACME HTTP challenge), so no other `docker compose up` needs to
# happen first. Safe to re-run: if a real certificate already exists it
# skips straight to just making sure the stack is up.
#
# After this succeeds, the `certbot` service in docker-compose.yml keeps the
# certificate renewed for as long as the stack is running, and frontend
# reloads nginx every 6h (frontend/start.sh) to pick up renewals.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DOMAIN="${DOMAIN:-controlroom.point2point.org.uk}"
EMAIL="${LETSENCRYPT_EMAIL:-sergio.molino@point2point.org.uk}"
STAGING="${STAGING:-0}"  # set to 1 to use Let's Encrypt's staging server (no rate limits, untrusted cert) while testing

LIVE_PATH="/etc/letsencrypt/live/$DOMAIN"

# Runs a command inside a throwaway container built from the `certbot`
# service (so it shares its certbot-conf/certbot-www volume mounts),
# overriding the entrypoint so we can run plain shell/openssl/certbot.
certbot_run() {
  local entrypoint="$1"
  shift
  docker compose run --rm --entrypoint "$entrypoint" certbot "$@"
}

echo "### Domain: $DOMAIN"
echo "### Email:  $EMAIL"

if certbot_run sh -c "[ -f $LIVE_PATH/fullchain.pem ]"; then
  echo "### Certificate already exists for $DOMAIN, skipping issuance."
  docker compose up -d backend frontend
  exit 0
fi

echo "### Creating a temporary self-signed certificate for $DOMAIN..."
echo "### (nginx needs *some* cert at this path to start; the real one replaces it below)"
certbot_run sh -c "
  set -e
  mkdir -p '$LIVE_PATH'
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout '$LIVE_PATH/privkey.pem' \
    -out '$LIVE_PATH/fullchain.pem' \
    -subj '/CN=$DOMAIN'
"

echo "### Starting backend + frontend..."
docker compose up -d backend frontend

echo "### Deleting the temporary certificate..."
certbot_run sh -c "
  rm -rf '/etc/letsencrypt/live/$DOMAIN' \
         '/etc/letsencrypt/archive/$DOMAIN' \
         '/etc/letsencrypt/renewal/$DOMAIN.conf'
"

echo "### Requesting the real Let's Encrypt certificate for $DOMAIN..."
staging_arg=""
if [ "$STAGING" = "1" ]; then
  staging_arg="--staging"
fi
certbot_run certbot certonly --webroot -w /var/www/certbot \
  --email "$EMAIL" -d "$DOMAIN" \
  --rsa-key-size 2048 --agree-tos --no-eff-email --non-interactive $staging_arg

echo "### Reloading nginx..."
docker compose exec frontend nginx -s reload

echo "### Done. https://$DOMAIN should now serve a trusted certificate."
