#!/bin/bash
# DEPRECATED - this file is no longer needed.
#
# The original plan was to recreate the existing `mina` container (the block
# producer) with an --archive-address flag. That would have meant downtime for
# the producer, which is not acceptable.
#
# Instead docker-compose.yml brings up a separate non-producing mina-follower
# that syncs the chain itself and feeds blocks to the archive. The producer is
# left completely untouched.
#
# See README.md.
exit 1
