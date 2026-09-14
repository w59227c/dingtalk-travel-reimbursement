#!/bin/sh
set -eu

config="nginx/default.conf"
proxy="nginx/expense-proxy.conf"

for required in \
    "server_tokens off" \
    "Content-Security-Policy" \
    "frame-src 'self' blob:" \
    "X-Content-Type-Options" \
    "Cache-Control" \
    "client_body_temp_path /var/cache/nginx/client_temp" \
    "proxy_temp_path /var/cache/nginx/proxy_temp" \
    "proxy_request_buffering off" \
    "client_max_body_size 25m"
do
    grep -F "$required" "$config" >/dev/null
done

if [ "$(grep -Fc 'proxy_request_buffering off' "$config")" -ne 1 ]; then
    echo "The persistent upload route must disable request buffering." >&2
    exit 1
fi

grep -F 'location ~ ^/api/reimbursements/drafts/[^/]+/files$' "$config" >/dev/null

for required in \
    "proxy_pass http://backend:8000" \
    "proxy_set_header X-Forwarded-Proto" \
    "proxy_set_header X-Request-ID" \
    "proxy_hide_header Cache-Control" \
    "proxy_max_temp_file_size 0" \
    "proxy_read_timeout 360s" \
    "proxy_send_timeout 360s"
do
    grep -F "$required" "$proxy" >/dev/null
done

if grep -Eq 'listen[[:space:]]+443|ssl_certificate' "$config" "$proxy"; then
    echo "Application Nginx must not contain TLS material; terminate HTTPS upstream." >&2
    exit 1
fi

if grep -Eq 'limit_req|limit_req_zone|\$proxy_add_x_forwarded_for' "$config" "$proxy"; then
    echo "Application Nginx must not rate-limit or trust caller-provided forwarding chains." >&2
    exit 1
fi

for required in \
    'read_only: true' \
    'no-new-privileges:true' \
    '/var/cache/nginx:uid=101,gid=101,mode=0700,size=33554432' \
    '/var/run:uid=101,gid=101,mode=0700,size=1048576'
do
    grep -F "$required" docker-compose.yml >/dev/null
done

echo "Nginx deployment policy checks passed"
