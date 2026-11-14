#!/bin/sh
# Generates a self-signed TLS certificate at container start (never stored in git).
set -e
CERT_DIR=/etc/nginx/certs
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/tls.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
    -subj "/CN=filegrid.local/O=FileGrid" \
    -addext "subjectAltName=DNS:localhost,DNS:nginx,DNS:filegrid.local,IP:127.0.0.1" \
    >/dev/null 2>&1
  chmod 600 "$CERT_DIR/tls.key"
  echo "40-gen-cert: self-signed certificate generated"
fi
# Publish ONLY the public certificate on a shared volume, so internal clients
# (the meter simulator) can verify the gateway instead of disabling TLS checks.
# The private key never leaves this container.
if [ -d /etc/nginx/ca ]; then
  cp "$CERT_DIR/tls.crt" /etc/nginx/ca/tls.crt
  chmod 644 /etc/nginx/ca/tls.crt
fi
