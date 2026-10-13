#!/bin/sh
# Idempotent store setup: safe to run on every install/upgrade.
set -eu
cd /var/www/html

log() { echo "[bootstrap] $*"; }
wpa() { wp "$@" --user="$WP_ADMIN_USER"; }

log "waiting for WordPress files on the volume"
until [ -f wp-config.php ]; do sleep 3; done

log "waiting for MySQL"
until php -r 'mysqli_report(MYSQLI_REPORT_OFF); [$h, $p] = array_pad(explode(":", getenv("WORDPRESS_DB_HOST")), 2, 3306);
  exit(@mysqli_connect($h, getenv("WORDPRESS_DB_USER"), getenv("WORDPRESS_DB_PASSWORD"), getenv("WORDPRESS_DB_NAME"), (int) $p) ? 0 : 1);'; do
  sleep 3
done

if ! wp core is-installed 2>/dev/null; then
  log "installing WordPress at $WP_URL"
  wp core install --url="$WP_URL" --title="$STORE_TITLE" --admin_user="$WP_ADMIN_USER" \
    --admin_password="$WP_ADMIN_PASSWORD" --admin_email="$WP_ADMIN_EMAIL" --skip-email
fi
wp option update blogname "$STORE_TITLE"
wp option update timezone_string "$STORE_TIMEZONE"
wp rewrite structure '/%postname%/'
wp rewrite flush

current="$(wp plugin get woocommerce --field=version 2>/dev/null || true)"
if [ "$current" != "$WOO_VERSION" ]; then
  log "installing WooCommerce $WOO_VERSION (current: ${current:-none})"
  wp plugin install woocommerce --version="$WOO_VERSION" --force
fi
wp plugin is-active woocommerce || wp plugin activate woocommerce
wp wc update

log "configuring store: currency, coming-soon off, Cash on Delivery, pages, free shipping"
wp option update woocommerce_currency "$STORE_CURRENCY"
wp option update woocommerce_default_country "$STORE_COUNTRY"
wp option update woocommerce_coming_soon no
wp option update woocommerce_store_pages_only no
wp option update woocommerce_enable_guest_checkout yes
wp option update woocommerce_cod_settings --format=json \
  '{"enabled":"yes","title":"Cash on delivery","description":"Pay with cash upon delivery.","instructions":"Pay with cash upon delivery.","enable_for_methods":[],"enable_for_virtual":"yes"}'
wpa wc tool run install_pages
if [ "$(wpa wc shipping_zone_method list 0 --format=count)" = "0" ]; then
  wpa wc shipping_zone_method create 0 --method_id=free_shipping
fi

log "ensuring dedicated API user 'mcp-bot' (shop_manager) and the woo-mcp REST key"
if ! wp user get mcp-bot --field=ID >/dev/null 2>&1; then
  wp user create mcp-bot mcp-bot@urumi.local --role=shop_manager --user_pass="$(head -c 24 /dev/urandom | base64)"
fi
wp eval-file /scripts/register-api-key.php

if [ "$SAMPLE_PRODUCTS" = "true" ] && [ "$(wp post list --post_type=product --post_status=any --format=count)" = "0" ]; then
  log "seeding sample products"
  wpa wc product create --name="Urumi T-Shirt" --regular_price="19.99" --status=publish
  wpa wc product create --name="Kubernetes Mug" --regular_price="12.50" --status=publish
  wpa wc product create --name="MCP Sticker Pack" --regular_price="4.99" --status=publish
fi

log "done: store ready at $WP_URL"
