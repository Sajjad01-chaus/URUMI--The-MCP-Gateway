<?php
// Upsert the woo-mcp REST API key from the K8s Secret (K8s is the source of truth for the credential).
global $wpdb;

$consumer_key    = getenv('WOO_CONSUMER_KEY');
$consumer_secret = getenv('WOO_CONSUMER_SECRET');
$user            = get_user_by('login', 'mcp-bot');

if (!$consumer_key || !$consumer_secret || !$user) {
    WP_CLI::error('missing WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET or mcp-bot user');
}

$table = $wpdb->prefix . 'woocommerce_api_keys';
$data  = [
    'user_id'         => $user->ID,
    'description'     => 'woo-mcp',
    'permissions'     => 'read_write',
    'consumer_key'    => wc_api_hash($consumer_key),
    'consumer_secret' => $consumer_secret,
    'truncated_key'   => substr($consumer_key, -7),
];

$key_id = $wpdb->get_var($wpdb->prepare("SELECT key_id FROM {$table} WHERE description = %s", 'woo-mcp'));
if ($key_id) {
    $wpdb->update($table, $data, ['key_id' => $key_id]);
    WP_CLI::success("updated woo-mcp API key (id {$key_id})");
} else {
    $wpdb->insert($table, $data);
    WP_CLI::success("created woo-mcp API key (id {$wpdb->insert_id})");
}
