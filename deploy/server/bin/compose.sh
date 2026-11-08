#!/usr/bin/env bash
# Open Persona: docker compose for this host, the way the kit's scripts run it: the host
# profile applied (hosts/<profile>.env, through lib/compose.sh), the env and data paths
# set. The RUNBOOK's manual commands use it, for example:
#
#   bin/compose.sh ps
#   bin/compose.sh logs -f api
#   bin/compose.sh exec api bash
#   bin/compose.sh run --rm migrate
#
# Never paste the output of `bin/compose.sh config`: it prints every secret in the env files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
export OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
export OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"
export OP_IMAGE_TAG="${OP_IMAGE_TAG:-current}"

# shellcheck source=lib/compose.sh
. "${SCRIPT_DIR}/lib/compose.sh"
compose "$@"
