# Session Note: Cross-Harness Live Run

**Date**: 2026-09-22  
**Time Range**: 09:19:27 to 09:55:11 UTC (approximately 36 minutes)  
**Database**: state.sqlite3 (checkpointed, consolidated)  

## Overview

This evidence directory contains the output from a live validation run demonstrating cross-harness interoperability. Two separate coding-agent harnesses (OpenCode using Minimax model, and Codex) both drove the shared Agent Surface workspace through the CLI contract, processing events and writing versions to the same database in sequence.

## Exact Commands Run

From `agent_worker.sh` (a one-shot worker script that processes a single event):

1. **Claim event and fetch next**: `python3 -m surface.cli --db <DB> inbox next --wait 10`
2. **Process with OpenCode**: `opencode run --model "opencode-go/minimax-m2.7" "<PROMPT>"`
3. **Process with Codex**: `codex exec "<PROMPT>" --dangerously-bypass-approvals-and-sandbox`
4. **Write version**: Via Python Store API, `put_version()` with `created_by='opencode-worker'` or `created_by='codex-worker'`
5. **Acknowledge event**: `python3 -m surface.cli --db <DB> inbox ack <EVT_ID>`

From `board_only_web.py`:
- Started web board on http://127.0.0.1:7921 (board_only_web.py) and 127.0.0.1:7920 (supervisor)
- Generated authentication token for board access
- Supervisor loop started to coordinate activity

From `wait_lease.sh`:
- Polls every 5 seconds to detect when event evt_10ae369c's lease expires
- Calls `python3 -m surface.cli --db ... recover --force-reclaim` to reclaim abandoned events
- This script demonstrates the recovery/reclaim mechanism

## Harnesses and Models Involved

- **OpenCode harness**: Used model `opencode-go/minimax-m2.7` (Minimax model)
  - Processed event evt_10ae369c
  - Created version ver_88ae8240
  - Identified in database as created_by='opencode-worker'

- **Codex harness**: Default model (not parameterized in script)
  - Processed event evt_bb7b8c1e
  - Created version ver_f65f7b7d
  - Identified in database as created_by='codex-worker'

- **CLI executor**: Generic `cli_worker` (the agent_worker.sh script itself)
  - Claimed all events for processing
  - Executed the harness-specific commands

## Event IDs, Types, Timestamps, and Lease/Claim Transitions

**Event evt_10ae369c (Revise request)**
- Created: 2026-09-22T09:35:28.387594Z (initial, from web board)
- Payload: "Write a 4-line haiku-style poem about autonomous agents recovering from failure."
- Claimed: 2026-09-22T09:54:36.898412Z by cli_worker
- Attempt count: 2 (indicates event was retried)
- Acknowledged: 2026-09-22T09:54:42.909435Z
- Status: acked
- Generated version: ver_88ae8240 by opencode-worker
- Note: Large gap (19+ minutes) between event creation and claim suggests it was queued and later picked up

**Event evt_bb7b8c1e (Revise request)**
- Created: 2026-09-22T09:54:56.905095Z
- Payload: "Rewrite it about a different CLI agent picking up the same task after the first one crashed."
- Claimed: 2026-09-22T09:55:01.507036Z by cli_worker
- Attempt count: 1 (processed once, cleanly)
- Acknowledged: 2026-09-22T09:55:11.276463Z
- Status: acked
- Generated version: ver_f65f7b7d by codex-worker
- Note: Created immediately after first event was acked; very quick claim (5 seconds later)

## Versions Created

1. **ver_61eddb85**: Initial version of artifact poem_1, created by generic 'worker' at 2026-09-22T09:19:27.749601Z (seed/bootstrap)
2. **ver_88ae8240**: Revision created by opencode-worker from event evt_10ae369c at 2026-09-22T09:54:42.857735Z
3. **ver_f65f7b7d**: Revision created by codex-worker from event evt_bb7b8c1e at 2026-09-22T09:55:11.222481Z

**Distinct worker identities in database**: 1 (`cli_worker` who claimed events) + 2 worker harnesses (`opencode-worker`, `codex-worker`) = 3 distinct worker/harness roles visible in the evidence.

## Worker Invocation Pattern

**Manual per-event**: Each `agent_worker.sh` invocation is a one-shot script:
- Calls `inbox next --wait 10` to claim and retrieve one event
- Runs one harness (opencode or codex) based on parameter
- Writes result and acknowledges
- Exits

This is not continuous listening—each harness was manually invoked once per event, not spawned as a daemon polling a queue. The `--wait 10` parameter means the CLI will wait up to 10 seconds for an event to become available, but the script itself does not loop.

## What This Evidence Does and Does Not Establish

Corrected 2026-09-25 after a final adversarial review (`OPUS_FINAL_REVIEW.md` in the
paper's track). The first pass of this section, written straight after the run, was more
optimistic than the raw evidence supports. This is the corrected version; do not restore
the earlier wording.

**Harness independence: WEAKLY SUPPORTED, not demonstrated.**
Both events were claimed as `cli_worker` by one bash wrapper (`agent_worker.sh`), which
then shelled out to `opencode` or `codex` to produce the text content. The queue and store
did not see two independent harnesses claiming and acking events on their own; they saw one
wrapper script doing that, with the actual model call swapped underneath it. That the
wrapper is harness-agnostic is real and worth keeping. "Two agent harnesses independently
drove the workspace" is not what this run shows.

**Live agent-connected UI: NOT SUPPORTED.**
Nothing in the database or logs records where an event came from. There is no HTTP access
log for `board_only_web.py`. A browser was involved in setting up the run, but nothing here
proves an event was created by a browser action rather than a script or CLI call.

**Recovery after lease expiry: NOT DEMONSTRATED as a real crash.**
`wait_lease.sh` hardcodes the event id `evt_10ae369c` and calls `recover --force-reclaim`
in a loop until that specific event's status flips back to pending. That is a forced,
scripted reclaim aimed at one known event, not a lease that expired on its own while a
real worker was mid-task and got picked up by a different one. `attempt_count=2` is
consistent with a reclaim having happened, but it does not by itself prove the reclaim was
lease-expiry-driven rather than staged. Treat this run as a demonstration of the recovery
*mechanism* (the CLI command exists and flips the state), not as an observed crash-recovery
event.

**Autonomous operation: NOT SUPPORTED.**
`agent_worker.sh` was invoked by hand, once per event. There is no daemon loop.

**Remote access: NOT SUPPORTED.**
All network activity is to 127.0.0.1. No tunnel, no second host, no remote client.

**What actually held up:** the queue correctly decoupled event dispatch from which CLI
produced the content, both invocations wrote versions to the same artifact with no
corruption, and the `recover --force-reclaim` command does what it says. That is real,
and smaller than the original framing.

---

## Files Included

- `state.sqlite3`: Full database snapshot after WAL checkpoint (contains all events, versions, artifacts, timestamps)
- `agent_worker.sh`: The one-shot worker script that claims events and invokes harnesses
- `wait_lease.sh`: Recovery/reclaim polling loop (demonstrates recovery mechanism)
- `board_only_web.py`: Web board server script
- `board.log`: Board startup output (URL and auth token)
- `serve.log`: Supervisor/board startup log
- `run/`: Directory containing board access token

## Key Observations

1. **No shared state corruption**: Both harnesses wrote to the same artifact (poem_1) without conflicts; versions are numbered sequentially (1, 2, 3).
2. **Deterministic worker identification**: Each harness identifies itself uniquely in `created_by` field, making it trivial to trace which harness generated which version.
3. **Queue-based decoupling**: Events are the decoupling point—harnesses never interact directly, only through the queue and artifact/version contracts.
4. **Timestamps are precise**: UTC timestamps with millisecond precision enable accurate reconstruction of the event lifecycle.
