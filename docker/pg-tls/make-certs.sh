#!/bin/sh
# Test-only TLS material for the PostgreSQL 17 test container: a CA, and a server certificate for
# 127.0.0.1 / localhost signed by it (so tls: verify-full can be tested). Never use outside tests.
set -eu
cd "$(dirname "$0")"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=sql-mcp test CA" -keyout ca.key -out ca.crt 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" -keyout server.key -out server.csr 2>/dev/null
printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\n' > san.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 3650 -extfile san.ext -out server.crt 2>/dev/null
rm -f server.csr san.ext ca.srl
echo "wrote ca.crt, server.crt, server.key in $(pwd)"
