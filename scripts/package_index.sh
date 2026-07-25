#!/usr/bin/env bash
# Package the portable index for transfer to the query machine (WP4/WP5).
#
# Everything the local app needs lives in `index/` + `extracted/`: LanceDB
# vectors, chunk JSON, the knowledge graph, the manifest, and the extracted text
# that citations resolve against. Nothing else is required — in particular no
# SARVAM_API_KEY, because querying makes no API calls.
#
#   ./scripts/package_index.sh            # writes kng-index-YYYYMMDD.tar.gz
#
# On the laptop:
#   sha256sum -c kng-index.sha256 && tar -xzf kng-index-*.tar.gz
set -euo pipefail

cd "$(dirname "$0")/.."
STAMP=$(date +%Y%m%d-%H%M)
OUT="kng-index-${STAMP}.tar.gz"

for d in index extracted; do
    [ -d "$d" ] || { echo "missing $d/ — run the pipeline first" >&2; exit 1; }
done

echo "packaging index/ + extracted/ ..."
tar -czf "$OUT" index/ extracted/
sha256sum "$OUT" > "${OUT}.sha256"

echo
echo "  archive : $OUT  ($(du -h "$OUT" | cut -f1))"
echo "  checksum: ${OUT}.sha256"
echo
echo "contents:"
du -sh index/lancedb index/chunks index/graph extracted 2>/dev/null | sed 's/^/  /'
echo
if [ -f index/graph/graph.json ]; then
    .venv/bin/python - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
g = json.loads(Path("index/graph/graph.json").read_text())
print(f"  graph   : {len(g.get('nodes', []))} nodes / {len(g.get('edges', []))} edges")
PY
fi
echo
echo "copy to the laptop, then:"
echo "  sha256sum -c ${OUT}.sha256 && tar -xzf $OUT"
echo "  pip install -e '.[local]'"
echo "  python -m kng.graph_query stats"
