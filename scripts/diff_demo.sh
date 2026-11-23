#!/usr/bin/env bash
# diff-runner demo: run the full pipeline on v1 of the Shelfwise docs, then apply
# the v2 docs + proto and let diff-runner update only what changed.
#   scripts/diff_demo.sh            # uses Claude if credentials exist
#   scripts/diff_demo.sh --offline  # no API calls
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO=examples/diff-demo
FLAGS="--auto-approve ${1:-}"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"   # so the launcher can import sample_service

rm -rf "$DEMO/workspace" && mkdir -p "$DEMO/workspace"
cp -r sample_service/docs "$DEMO/workspace/docs"
cp sample_service/protos/shelfwise.proto "$DEMO/workspace/shelfwise.proto"

echo "== 1. full pipeline on v1 =="
rga run -c "$DEMO/pipeline_config.json" $FLAGS || true

echo "== 2. docs and API change (v2) =="
rm -rf "$DEMO/workspace/docs" && cp -r sample_service/docs_v2 "$DEMO/workspace/docs"
cp sample_service/protos/shelfwise_v2.proto "$DEMO/workspace/shelfwise.proto"

echo "== 3. diff-runner =="
rga diff -c "$DEMO/pipeline_config.json" $FLAGS || true
echo
cat "$DEMO/workspace/output/diff_report.md"
