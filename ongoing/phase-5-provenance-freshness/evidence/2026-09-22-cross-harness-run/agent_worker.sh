#!/bin/bash
# WARNING: this is a raw, unedited artifact of one manual live-validation run.
# It is not a shipped example and must not be copied into production code or
# docs as-is: it pastes LLM output directly into a Python string literal
# (code-injection pattern) and runs codex with
# --dangerously-bypass-approvals-and-sandbox. Kept here only as evidence of
# what was actually run, so the session note stays checkable.
#
# Real-agent worker loop: claim -> generate (via CLI agent) -> write -> ack.
# Usage: agent_worker.sh <db_path> <claim_cmd_worker_name>
set -e
SB="<session-scratchpad>"
DB="$1"
CLI="$2"          # e.g. "opencode" or "codex"
MODEL="$3"        # e.g. "opencode-go/minimax-m2.7" (ignored for codex)

cd <repo-root>

EVT_JSON=$(python3 -m surface.cli --db "$DB" inbox next --wait 10)
echo "$EVT_JSON"
EVT_ID=$(echo "$EVT_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))")
ARTIFACT_ID=$(echo "$EVT_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('artifact_id',''))")
NOTE=$(echo "$EVT_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('payload',{}).get('note',''))")

if [ -z "$EVT_ID" ] || [ "$EVT_ID" == "None" ]; then
  echo "NO_EVENT"
  exit 1
fi

PROMPT="Write only the final text, no preamble/markdown fences. $NOTE"

if [ "$CLI" == "opencode" ]; then
  CONTENT=$(opencode run --model "$MODEL" "$PROMPT" 2>/dev/null)
elif [ "$CLI" == "codex" ]; then
  CONTENT=$(codex exec "$PROMPT" --dangerously-bypass-approvals-and-sandbox 2>/dev/null | tail -30)
else
  echo "UNKNOWN_CLI"
  exit 1
fi

echo "--- generated content ($CLI) ---"
echo "$CONTENT"

python3 -c "
from surface.store import Store
s = Store('$DB')
s.put_version('$ARTIFACT_ID', content='''$CONTENT''', content_type='text/markdown', created_by='$CLI-worker', source_event_id='$EVT_ID', note='revised by $CLI ($MODEL)')
"
python3 -m surface.cli --db "$DB" inbox ack "$EVT_ID"
echo "ACKED $EVT_ID by $CLI"
