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

log "publishing the storefront homepage"
homepage_file="$(mktemp)"
cat > "$homepage_file" <<'HTML'
<!-- wp:html -->
<style>
body.home main{margin-block-start:0!important}body.home main>.wp-block-group{padding-top:0!important;padding-bottom:0!important}body.home .entry-content{margin-block-start:0!important}body.home .wp-block-post-title,body.home footer.wp-block-template-part{display:none}.urumi-hero{border-radius:0 0 28px 28px;box-shadow:0 24px 70px rgba(15,23,42,.18);overflow:hidden}.urumi-kicker{letter-spacing:.16em;text-transform:uppercase}.urumi-feature{border:1px solid #e2e8f0;border-radius:18px;height:100%;box-shadow:0 10px 30px rgba(15,23,42,.06)}.urumi-products{max-width:none!important}.urumi-products ul.products{display:grid!important;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}.urumi-products ul.products::before,.urumi-products ul.products::after{display:none!important}.urumi-products li.product{border:1px solid #e2e8f0;border-radius:18px;padding:18px!important;margin:0!important;width:auto!important;box-shadow:0 10px 30px rgba(15,23,42,.06)}@media(max-width:760px){.urumi-products ul.products{grid-template-columns:1fr}}
</style>
<!-- /wp:html -->
<!-- wp:group {"align":"full","className":"urumi-hero","style":{"color":{"gradient":"linear-gradient(135deg,rgb(15,23,42) 0%,rgb(49,46,129) 58%,rgb(124,58,237) 100%)"},"spacing":{"padding":{"top":"96px","right":"clamp(28px,7vw,110px)","bottom":"96px","left":"clamp(28px,7vw,110px)"}}},"textColor":"base","layout":{"type":"constrained"}} -->
<div class="wp-block-group alignfull urumi-hero has-base-color has-text-color has-background" style="background:linear-gradient(135deg,rgb(15,23,42) 0%,rgb(49,46,129) 58%,rgb(124,58,237) 100%);padding-top:96px;padding-right:clamp(28px,7vw,110px);padding-bottom:96px;padding-left:clamp(28px,7vw,110px)"><!-- wp:paragraph {"className":"urumi-kicker","style":{"typography":{"fontSize":"13px","fontWeight":"700"}},"textColor":"accent-3"} -->
<p class="urumi-kicker has-accent-3-color has-text-color" style="font-size:13px;font-weight:700">URUMI DEMO STORE</p>
<!-- /wp:paragraph -->
<!-- wp:heading {"level":1,"style":{"typography":{"fontSize":"clamp(46px,8vw,88px)","lineHeight":"0.98","fontWeight":"700"},"spacing":{"margin":{"top":"18px","bottom":"28px"}}},"textColor":"base"} -->
<h1 class="wp-block-heading has-base-color has-text-color" style="margin-top:18px;margin-bottom:28px;font-size:clamp(46px,8vw,88px);font-weight:700;line-height:0.98">Commerce,<br>orchestrated.</h1>
<!-- /wp:heading -->
<!-- wp:paragraph {"style":{"typography":{"fontSize":"20px","lineHeight":"1.6"},"spacing":{"margin":{"bottom":"34px"}}},"textColor":"base"} -->
<p class="has-base-color has-text-color" style="margin-bottom:34px;font-size:20px;line-height:1.6">A production-style WooCommerce storefront running on Kubernetes and connected to a secure, multi-server MCP gateway.</p>
<!-- /wp:paragraph -->
<!-- wp:buttons -->
<div class="wp-block-buttons"><!-- wp:button {"backgroundColor":"base","textColor":"contrast","style":{"border":{"radius":"999px"},"spacing":{"padding":{"left":"28px","right":"28px","top":"14px","bottom":"14px"}}}} -->
<div class="wp-block-button"><a class="wp-block-button__link has-contrast-color has-base-background-color has-text-color has-background wp-element-button" href="/shop/" style="border-radius:999px;padding-top:14px;padding-right:28px;padding-bottom:14px;padding-left:28px">Shop the collection</a></div>
<!-- /wp:button -->
<!-- wp:button {"className":"is-style-outline","style":{"border":{"radius":"999px"},"spacing":{"padding":{"left":"28px","right":"28px","top":"14px","bottom":"14px"}}}} -->
<div class="wp-block-button is-style-outline"><a class="wp-block-button__link wp-element-button" href="/cart/" style="border-radius:999px;padding-top:14px;padding-right:28px;padding-bottom:14px;padding-left:28px">View cart</a></div>
<!-- /wp:button --></div>
<!-- /wp:buttons --></div>
<!-- /wp:group -->
<!-- wp:group {"align":"wide","style":{"spacing":{"padding":{"top":"72px","bottom":"28px"}}},"layout":{"type":"constrained"}} -->
<div class="wp-block-group alignwide" style="padding-top:72px;padding-bottom:28px"><!-- wp:heading {"textAlign":"center","style":{"typography":{"fontSize":"42px"}}} -->
<h2 class="wp-block-heading has-text-align-center" style="font-size:42px">Built for the demo. Ready for checkout.</h2>
<!-- /wp:heading -->
<!-- wp:paragraph {"align":"center","style":{"typography":{"fontSize":"18px"}},"textColor":"contrast-2"} -->
<p class="has-text-align-center has-contrast-2-color has-text-color" style="font-size:18px">Real products, persistent orders and agent-accessible commerce tools—all behind one live storefront.</p>
<!-- /wp:paragraph --></div>
<!-- /wp:group -->
<!-- wp:columns {"align":"wide","style":{"spacing":{"blockGap":{"left":"24px"},"margin":{"bottom":"72px"}}}} -->
<div class="wp-block-columns alignwide" style="margin-bottom:72px"><!-- wp:column -->
<div class="wp-block-column"><!-- wp:group {"className":"urumi-feature","style":{"spacing":{"padding":{"top":"28px","right":"28px","bottom":"28px","left":"28px"}}},"backgroundColor":"base","layout":{"type":"constrained"}} -->
<div class="wp-block-group urumi-feature has-base-background-color has-background" style="padding-top:28px;padding-right:28px;padding-bottom:28px;padding-left:28px"><!-- wp:heading {"level":3} --><h3 class="wp-block-heading">Secure checkout</h3><!-- /wp:heading --><!-- wp:paragraph --><p>Complete a real WooCommerce order over HTTPS with guest checkout and Cash on Delivery.</p><!-- /wp:paragraph --></div><!-- /wp:group --></div>
<!-- /wp:column -->
<!-- wp:column -->
<div class="wp-block-column"><!-- wp:group {"className":"urumi-feature","style":{"spacing":{"padding":{"top":"28px","right":"28px","bottom":"28px","left":"28px"}}},"backgroundColor":"base","layout":{"type":"constrained"}} -->
<div class="wp-block-group urumi-feature has-base-background-color has-background" style="padding-top:28px;padding-right:28px;padding-bottom:28px;padding-left:28px"><!-- wp:heading {"level":3} --><h3 class="wp-block-heading">MCP connected</h3><!-- /wp:heading --><!-- wp:paragraph --><p>Products and orders are exposed through permission-aware tools without revealing store credentials.</p><!-- /wp:paragraph --></div><!-- /wp:group --></div>
<!-- /wp:column -->
<!-- wp:column -->
<div class="wp-block-column"><!-- wp:group {"className":"urumi-feature","style":{"spacing":{"padding":{"top":"28px","right":"28px","bottom":"28px","left":"28px"}}},"backgroundColor":"base","layout":{"type":"constrained"}} -->
<div class="wp-block-group urumi-feature has-base-background-color has-background" style="padding-top:28px;padding-right:28px;padding-bottom:28px;padding-left:28px"><!-- wp:heading {"level":3} --><h3 class="wp-block-heading">Kubernetes native</h3><!-- /wp:heading --><!-- wp:paragraph --><p>Each store runs as an isolated Helm release with persistent data, health checks and network policy.</p><!-- /wp:paragraph --></div><!-- /wp:group --></div>
<!-- /wp:column --></div>
<!-- /wp:columns -->
<!-- wp:group {"align":"wide","style":{"spacing":{"padding":{"bottom":"80px"}}},"layout":{"type":"constrained"}} -->
<div class="wp-block-group alignwide" style="padding-bottom:80px"><!-- wp:heading {"style":{"typography":{"fontSize":"38px"},"spacing":{"margin":{"bottom":"28px"}}}} -->
<h2 class="wp-block-heading" style="margin-bottom:28px;font-size:38px">Featured products</h2>
<!-- /wp:heading -->
<!-- wp:shortcode -->[products limit="6" columns="3" orderby="date" order="DESC" class="urumi-products"]<!-- /wp:shortcode --></div>
<!-- /wp:group -->
<!-- wp:group {"align":"full","style":{"color":{"background":"#0f172a"},"spacing":{"padding":{"top":"48px","right":"clamp(28px,7vw,110px)","bottom":"48px","left":"clamp(28px,7vw,110px)"}}},"textColor":"base","layout":{"type":"flex","flexWrap":"wrap","justifyContent":"space-between"}} -->
<div class="wp-block-group alignfull has-base-color has-text-color has-background" style="background-color:#0f172a;padding-top:48px;padding-right:clamp(28px,7vw,110px);padding-bottom:48px;padding-left:clamp(28px,7vw,110px)"><!-- wp:group {"layout":{"type":"constrained"}} -->
<div class="wp-block-group"><!-- wp:heading {"level":3,"textColor":"base"} --><h3 class="wp-block-heading has-base-color has-text-color">Urumi Demo Store</h3><!-- /wp:heading --><!-- wp:paragraph {"style":{"color":{"text":"#cbd5e1"}}} --><p class="has-text-color" style="color:#cbd5e1">WooCommerce on Kubernetes · Connected through the Urumi MCP Gateway</p><!-- /wp:paragraph --></div>
<!-- /wp:group -->
<!-- wp:navigation {"overlayMenu":"never","textColor":"base","layout":{"type":"flex","justifyContent":"right"}} --><!-- wp:navigation-link {"label":"Shop","url":"/shop/","kind":"custom"} /--><!-- wp:navigation-link {"label":"Cart","url":"/cart/","kind":"custom"} /--><!-- wp:navigation-link {"label":"My account","url":"/my-account/","kind":"custom"} /--><!-- /wp:navigation --></div>
<!-- /wp:group -->
HTML
homepage_id="$(wp post list --post_type=page --name=urumi-store-home --field=ID --format=ids)"
if [ -n "$homepage_id" ]; then
  wp post update "$homepage_id" "$homepage_file" --post_title="Store" --post_status=publish
else
  homepage_id="$(wp post create "$homepage_file" --post_type=page --post_title="Store" --post_name=urumi-store-home --post_status=publish --porcelain)"
fi
rm -f "$homepage_file"
wp option update show_on_front page
wp option update page_on_front "$homepage_id"

for default_slug in hello-world sample-page; do
  default_ids="$(wp post list --post_type=any --name="$default_slug" --post_status=any --format=ids)"
  if [ -n "$default_ids" ]; then
    wp post delete $default_ids --force
  fi
done

log "done: store ready at $WP_URL"
