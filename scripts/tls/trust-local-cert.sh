#!/usr/bin/env bash
# Makes your machine trust FileGrid's gateway certificate (Linux / macOS), so https://localhost
# opens without a browser warning. Run it yourself, once; it asks for sudo.
#   scripts/tls/trust-local-cert.sh            add
#   scripts/tls/trust-local-cert.sh --remove   take it out again
# Only the PUBLIC certificate (valid for localhost) is added; the private key stays in nginx.
set -euo pipefail
cd "$(dirname "$0")/../.."
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
case "$(uname -s)" in
  Darwin)
    if [ "${1:-}" = "--remove" ]; then
      sudo security delete-certificate -c filegrid.local /Library/Keychains/System.keychain; exit 0
    fi
    docker compose cp nginx:/etc/nginx/certs/tls.crt "$TMP/filegrid.crt"
    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$TMP/filegrid.crt" ;;
  Linux)
    if [ "${1:-}" = "--remove" ]; then
      sudo rm -f /usr/local/share/ca-certificates/filegrid.crt && sudo update-ca-certificates --fresh; exit 0
    fi
    docker compose cp nginx:/etc/nginx/certs/tls.crt "$TMP/filegrid.crt"
    openssl x509 -in "$TMP/filegrid.crt" -noout -subject | grep -q "O *= *FileGrid" || { echo "not the FileGrid certificate" >&2; exit 1; }
    sudo cp "$TMP/filegrid.crt" /usr/local/share/ca-certificates/filegrid.crt && sudo update-ca-certificates
    echo "Chrome/Chromium on Linux keeps its own store: import the file in chrome://settings/certificates (Authorities)." ;;
  *) echo "Windows: use scripts\\tls\\trust-local-cert.ps1" >&2; exit 1 ;;
esac
echo "Trusted. Restart the browser, then open https://localhost"
