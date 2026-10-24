#!/bin/sh
# Restarts the production API pods after a config change.
set -eu
kubectl rollout restart deployment/api -n production
kubectl rollout status deployment/api -n production --timeout=120s
