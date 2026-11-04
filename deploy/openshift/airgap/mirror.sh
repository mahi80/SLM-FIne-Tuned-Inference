#!/usr/bin/env bash
# Mirror the platform images into an air-gapped registry.
#
#   PULL_SECRET=~/pull-secret.json MIRROR=mirror.internal.example \
#     deploy/openshift/airgap/mirror.sh
#
# The `oc image mirror` job runs on a connected host; the IDMS/ITMS objects are
# applied on the disconnected cluster. Model weights are a separate artifact:
# seed slm-model-cache from the mirrored revision before first start.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PULL_SECRET:?set PULL_SECRET to a registry auth file}"
: "${MIRROR:?set MIRROR to the internal registry host, no scheme}"
OC="${OC:-oc}"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed "s#@MIRROR@#${MIRROR}#g" "$here/images.txt" >"$tmp"

"$OC" image mirror -f "$tmp" --registry-config="$PULL_SECRET" --continue-on-error=true
