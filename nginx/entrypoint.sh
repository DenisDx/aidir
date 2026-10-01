#!/bin/sh
set -eu

: "${WEBUI_HOST:=host.docker.internal}"
: "${WEBUI_PORT:=20082}"
export WEBUI_HOST WEBUI_PORT

envsubst '${WEBUI_HOST} ${WEBUI_PORT}' \
  < /etc/nginx/templates/nginx.conf.template \
  > /etc/nginx/nginx.conf
nginx -t
exec nginx -g 'daemon off;'