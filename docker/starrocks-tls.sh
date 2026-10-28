#!/bin/sh
# Enable TLS on the StarRocks test FE (MySQL protocol, port 9030), TEST-ONLY:
#   * a JKS keystore with a self-signed certificate for 127.0.0.1 / localhost, made with the image's keytool
#   * fe.conf: ssl_keystore_location / ssl_keystore_password / ssl_key_password (documented since 3.4.1)
#   * the certificate is exported to docker/sr-tls/ca.pem, the CA a client verifies against
# Plaintext stays allowed (ssl_force_secure_transport is not set), so both paths can be tested.
set -eu
export PATH="$PATH:$HOME/.docker/bin"
C=sql-mcp-starrocks
CONF=/data/deploy/starrocks/fe/conf
docker exec "$C" sh -c "test -f $CONF/sqlmcp-test.jks || keytool -genkeypair -alias starrocks -keyalg RSA -keysize 2048 \
  -validity 3650 -dname CN=localhost -ext SAN=IP:127.0.0.1,DNS:localhost -storetype JKS \
  -keystore $CONF/sqlmcp-test.jks -storepass sqlmcp-test -keypass sqlmcp-test"
docker exec "$C" sh -c "grep -q '^ssl_keystore_location' $CONF/fe.conf || printf '\nssl_keystore_location = $CONF/sqlmcp-test.jks\nssl_keystore_password = sqlmcp-test\nssl_key_password = sqlmcp-test\n' >> $CONF/fe.conf"
docker exec "$C" keytool -exportcert -rfc -alias starrocks -keystore "$CONF/sqlmcp-test.jks" -storepass sqlmcp-test \
  > "$(dirname "$0")/sr-tls/ca.pem"
docker restart "$C" >/dev/null
echo "TLS configured; waiting for healthy..."
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$C")" = healthy ] && echo healthy && exit 0
  sleep 5
done
echo "not healthy after 5 minutes" >&2; exit 1
