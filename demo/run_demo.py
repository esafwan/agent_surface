"""
Full end-to-end demo of Agent Surface Board.
Movie pipeline: Script → Shots → Keyframes (mock image gen)

This script acts as both:
1. The "board UI" (injecting events into the inbox)
2. The "worker loop" (claiming + processing events with the reference worker)
"""
import sys, json
sys.path.insert(0, '/Users/safwan/Code/Experiments/agent_surface')

from surface.store import Store
from surface.stages.config import load_preset
from surface.worker import handle_event

DB = "/Users/safwan/Code/Experiments/agent_surface/.surface-board/state.sqlite3"
store = Store(DB)
stage_cfg = load_preset("movie")

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

def show_artifact(art_id):
    a = store.get_artifact(art_id)
    if not a:
        print(f"  ❌ {art_id} not found"); return
    status_icon = {"draft":"📝","review":"🔍","approved":"✅","stale":"⚠️","generating":"⏳","failed":"❌"}.get(a['status'],'❓')
    print(f"  {status_icon} [{a['status'].upper():10}] {a['id']:12} ({a['stage']:10}) | {a['title']}")
    if a.get('selected_version_id'):
        v = store.get_version(a['selected_version_id'])
        if v:
            preview = (v.get('content') or '')[:100].replace('\n', ' ')
            print(f"              v{v['n']}: \"{preview}...\"")

def pump_events(label="", max_events=15):
    """Process all pending events using the reference worker."""
    processed = 0
    for _ in range(max_events):
        evt = store.claim_next_event(worker_id="demo_worker", lease_seconds=30)
        if not evt:
            break
        art = store.get_artifact(evt.get('artifact_id')) if evt.get('artifact_id') else None
        # Build the event context the way supervisor does
        ctx_event = {
            "event_id": evt["id"],
            "type": evt["type"],
            "project_id": evt.get("project_id","demo"),
            "payload": evt.get("payload", {}),
            "artifact_id": evt.get("artifact_id"),
            "config": stage_cfg.raw,
            "artifact": art,
            "summary": {}
        }
        result = handle_event(ctx_event, store)
        if result.get('ok'):
            store.ack_event(evt['id'])
            icon = "✅"
        else:
            store.fail_event(evt['id'], result.get('error', 'unknown'))
            icon = f"❌ {result.get('error','?')}"
        print(f"    {icon} {evt['type']:15} → {evt.get('artifact_id','(project)')}")
        processed += 1
    if processed == 0:
        print("    (no pending events)")
    return processed

# ─────────────────────────────────────────────────────────────────────
section("STEP 1 — Initialize artifacts & dependency DAG")
# ─────────────────────────────────────────────────────────────────────

store.create_artifact("script_001", "script",    "The Last Signal - Script",    status="draft")
store.create_artifact("shots_001",  "shots",     "The Last Signal - Shot List",  status="draft")
store.create_artifact("kf_001",     "keyframes", "The Last Signal - Keyframes",  status="draft")

store.add_dependency("script_001", "shots_001")   # script → shots
store.add_dependency("shots_001",  "kf_001")      # shots → keyframes

print("  Artifacts created and DAG wired: script_001 → shots_001 → kf_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 2 — Write Script v1 (edit event)")
# ─────────────────────────────────────────────────────────────────────

SCRIPT_V1 = (
    "FADE IN:\n\n"
    "INT. ABANDONED RADIO STATION - NIGHT\n\n"
    "Dust motes float in pale moonlight. Ancient equipment lines the walls.\n\n"
    "ELENA (30s, worn jacket) sits at a console, headphones on, eyes closed.\n\n"
    "STATIC. Then -- a faint, rhythmic BEEP.\n\n"
    "Elena's eyes snap open.\n\n"
    "ELENA (whispering): That's not interference.\n\n"
    "She yanks a logbook from beneath the desk. Flips to a page dated 1987.\n\n"
    "CLOSE ON: a waveform diagram. Identical to what she hears now.\n\n"
    "She reaches for the transmit key.\n\n"
    "FADE TO BLACK.\n\nTITLE: THE LAST SIGNAL"
)

store.enqueue_event(
    type="edit",
    artifact_id="script_001",
    project_id="demo_movie",
    payload={"content": SCRIPT_V1, "content_type": "text/plain", "note": "First draft"}
)
pump_events("script edit v1")
show_artifact("script_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 3 — Approve the Script")
# ─────────────────────────────────────────────────────────────────────

store.enqueue_event(type="approve", artifact_id="script_001", project_id="demo_movie", payload={})
pump_events("approve script")
show_artifact("script_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 4 — Write Shot List (edit event)")
# ─────────────────────────────────────────────────────────────────────

SHOTS_V1 = (
    "SHOT LIST - THE LAST SIGNAL\n\n"
    "SHOT 1:  WS  - Exterior radio tower at night, moon behind clouds\n"
    "SHOT 2:  CU  - Elena's face, eyes closed, headphones on\n"
    "SHOT 3:  ECU - Oscilloscope: flat line\n"
    "SHOT 4:  ECU - Oscilloscope: sudden rhythmic spike (match cut)\n"
    "SHOT 5:  CU  - Elena's eyes snapping open\n"
    "SHOT 6:  MS  - Elena grabbing the logbook\n"
    "SHOT 7:  CU  - 1987 logbook page with waveform diagram\n"
    "SHOT 8:  OTS - Elena comparing live scope to diagram (MATCH!)\n"
    "SHOT 9:  CU  - Elena's hand reaching for transmit key\n"
    "SHOT 10: FADE TO BLACK"
)

store.enqueue_event(
    type="edit",
    artifact_id="shots_001",
    project_id="demo_movie",
    payload={"content": SHOTS_V1, "content_type": "text/plain", "note": "Initial shot list"}
)
pump_events("shots edit v1")
show_artifact("shots_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 5 — Revise Script v2 — watch stale propagation!")
# ─────────────────────────────────────────────────────────────────────

SCRIPT_V2 = SCRIPT_V1 + (
    "\n\n--- REVISED EPILOGUE ---\n\n"
    "A VOICE crackles through the static. Distorted, but unmistakably human.\n\n"
    "VOICE (V.O.): We've been waiting."
)

store.enqueue_event(
    type="revise",
    artifact_id="script_001",
    project_id="demo_movie",
    payload={"content": SCRIPT_V2, "content_type": "text/plain", "note": "Added voice response epilogue"}
)
pump_events("script revise v2")

print("\n  Checking downstream stale propagation:")
show_artifact("script_001")
show_artifact("shots_001")   # should be stale now
show_artifact("kf_001")      # should be stale now

# ─────────────────────────────────────────────────────────────────────
section("STEP 6 — Update shots to match revised script, then approve")
# ─────────────────────────────────────────────────────────────────────

SHOTS_V2 = SHOTS_V1 + (
    "\nSHOT 11: CU  - Radio speaker grille with subtle glow\n"
    "SHOT 12: CU  - Elena's face: recognition mixed with fear\n"
    "SHOT 13: SMASH CUT TO BLACK + VOICE V.O."
)

store.enqueue_event(
    type="revise",
    artifact_id="shots_001",
    project_id="demo_movie",
    payload={"content": SHOTS_V2, "content_type": "text/plain", "note": "Added epilogue shots"}
)
pump_events("shots revise v2")
store.enqueue_event(type="approve", artifact_id="shots_001", project_id="demo_movie", payload={})
pump_events("approve shots")
show_artifact("shots_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 7 — Trigger async image generation for keyframes")
# ─────────────────────────────────────────────────────────────────────

job = store.create_job(
    artifact_id="kf_001",
    provider="image_default",
    kind="image",
    request={"prompt": "Abandoned radio station at night, moonlight, oscilloscope glow, 1980s, cinematic 35mm film"}
)
store.set_status("kf_001", "generating")
print(f"  📤 Job queued: {job['id']} (provider=image_default)")

# Simulate poller completing the job
finished_job = store.finish_job(
    job_id=job['id'],
    result={
        "url": "mock://keyframe_radio_night_001.png",
        "content_type": "image/png",
        "width": 1920, "height": 1080,
        "note": "Mock-generated keyframe — radio station scene"
    }
)
print(f"  ⚡ Poller: job {job['id']} succeeded")

# The finish_job should emit a job_done event via the store
# Let's manually enqueue the job_done if not already emitted
store.enqueue_event(
    type="job_done",
    artifact_id="kf_001",
    project_id="demo_movie",
    payload={"job_id": job['id'], "result": finished_job.get('result',{})}
)
pump_events("job_done for keyframes")
show_artifact("kf_001")

# ─────────────────────────────────────────────────────────────────────
section("STEP 8 — Approve Keyframes")
# ─────────────────────────────────────────────────────────────────────

store.enqueue_event(type="approve", artifact_id="kf_001", project_id="demo_movie", payload={})
pump_events("approve keyframes")
show_artifact("kf_001")

# ─────────────────────────────────────────────────────────────────────
section("FINAL — Project Summary")
# ─────────────────────────────────────────────────────────────────────

all_artifacts = store.list_artifacts()
print(f"  Total artifacts: {len(all_artifacts)}\n")
for a in all_artifacts:
    versions = store.list_versions(a['id'])
    status_icon = {"draft":"📝","review":"🔍","approved":"✅","stale":"⚠️","generating":"⏳"}.get(a['status'],'❓')
    print(f"  {status_icon} {a['status'].upper():12} | {a['stage']:10} | {a['id']:12} | {len(versions)}v | {a['title']}")

approved_ids = {a['id'] for a in all_artifacts if a['status'] == 'approved'}
all_required = {"script_001", "shots_001", "kf_001"}
complete = all_required.issubset(approved_ids)
print(f"\n  Pipeline complete: {'🎬 YES!' if complete else '🚧 Not yet - missing: ' + str(all_required - approved_ids)}")
