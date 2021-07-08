#!/usr/bin/env bash

# Find and change to repo root directory
OS=$(uname)
if [[ "$OS" == "Darwin" ]]; then
	# OSX uses BSD readlink
	BASEDIR="$(dirname "$0")"
else
	BASEDIR=$(readlink -e "$(dirname "$0")")
fi
cd "${BASEDIR}/../../.." || exit 1

# This sample assumes that a local DSS instance is available similar to the one
# produced with /build/dev/run_locally.sh
AUTH="DummyOAuth(http://host.docker.internal:8085/token,uss1)"
DSS="http://host.docker.internal:8082"
PUBLIC_KEY="/var/test-certs/auth2.pem"
AUD="host.docker.internal"
BASE_URL="http://host.docker.internal:8072/host"
PORT=8072

docker build \
  -t local-interuss/rid-host \
  -f monitoring/rid_qualifier/host/Dockerfile \
  monitoring \
  || exit 1

docker run --name rid-host \
  --rm \
  -e MOCK_HOST_AUTH_SPEC="${AUTH}" \
  -e MOCK_HOST_DSS_URL="${DSS}" \
  -e MOCK_HOST_PUBLIC_KEY="${PUBLIC_KEY}" \
  -e MOCK_HOST_TOKEN_AUDIENCE="${AUD}" \
  -e MOCK_HOST_BASE_URL="${BASE_URL}" \
  -p ${PORT}:5000 \
  -v `pwd`/build/test-certs:/var/test-certs:ro \
  local-interuss/rid-host \
  gunicorn \
    --preload \
    --workers=1 \
    --bind=0.0.0.0:5000 \
    monitoring.rid_qualifier.host:webapp
