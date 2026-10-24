#!/bin/sh
# Run from the repository root (`./scripts/ci.sh`): it calls its sibling scripts by repo-relative paths.
set -eu
./scripts/build.sh
./scripts/upload.sh
