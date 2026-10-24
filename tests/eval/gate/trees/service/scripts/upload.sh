#!/bin/sh
# Ships the build to production (no deploy word in the file name).
set -eu
flyctl deploy --app bonvihr-prod --remote-only
