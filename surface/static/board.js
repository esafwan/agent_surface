// Agent Surface board client.
//
// The server is the source of truth: every repaint comes from /api/state,
// and the page derives nothing the display model did not already say. Two
// distinct kinds of "in flight" are shown, because they fail differently:
//
//   local  — this browser has a POST open right now (progress bar, disabled
//            controls, elapsed counter). It ends when the POST returns.
//   queued — the store holds a pending/processing event for this artifact.
//            It outlives the POST, survives a reload, and is the state that
//            used to be invisible: the click landed, nothing has claimed it.
//
// Every string from the store reaches the DOM as a text node, never
// innerHTML, so content keeps its newlines and a draft containing <script>
// renders as characters.

const $ = (id) => document.getElementById(id);

let state = null;
let activeStage = null;        // stage_id of the visible tab
let lastFingerprint = null;
let inflight = 0;              // open POST count
let inflightSince = 0;
let tickTimer = null;
const drafts = {};             // unsent input text, keyed "<artifact>:<slot>"
let notice = null;             // {text, ok} shown until the next action

// --- small DOM helpers ------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function draftKey(artifactId, slot) {
  return artifactId + ":" + slot;
}

function rememberInput(input, artifactId, slot) {
  const key = draftKey(artifactId, slot);
  input.dataset.key = key;
  if (drafts[key] !== undefined) input.value = drafts[key];
  input.addEventListener("input", () => { drafts[key] = input.value; });
  return input;
}

// --- mermaid helpers -------------------------------------------------------

// mermaid.js's own error path is NOT contained to the container you gave it:
// on a parse/render failure it appends its own error SVG (a bomb icon +
// "Syntax error in text") directly to document.body as a side effect,
// before the exception even reaches this file's try/catch -- so the caller
// catching the rejection and showing a text fallback (below) does not stop
// that debris from appearing, floating below the whole page. mermaid v10.3+
// exposes suppressErrorRendering specifically to turn this off; initialize
// it once, defensively, before the first render call.
let mermaidInitialized = false;
function ensureMermaidInitialized() {
  if (mermaidInitialized || !window.mermaid) return;
  try {
    window.mermaid.initialize({ startOnLoad: false, suppressErrorRendering: true });
  } catch (err) {
    console.warn('mermaid.initialize failed:', err);
  }
  mermaidInitialized = true;
}

// Extract mermaid fenced blocks from text. Returns an array of objects with
// { type: 'text' | 'mermaid', content: string }
function extractMermaidBlocks(text) {
  const blocks = [];
  const mermaidRegex = /```mermaid\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match;

  while ((match = mermaidRegex.exec(text)) !== null) {
    // Add text before this mermaid block
    if (match.index > lastIndex) {
      blocks.push({ type: 'text', content: text.substring(lastIndex, match.index) });
    }
    // Add the mermaid block
    blocks.push({ type: 'mermaid', content: match[1].trim() });
    lastIndex = match.index + match[0].length;
  }

  // Add any remaining text after the last mermaid block
  if (lastIndex < text.length) {
    blocks.push({ type: 'text', content: text.substring(lastIndex) });
  }

  // If no mermaid blocks were found, return the entire text as a single text block
  if (blocks.length === 0) {
    blocks.push({ type: 'text', content: text });
  }

  return blocks;
}

// Render a mermaid diagram into a container element. Returns true on success,
// false on failure (in which case the caller should render the raw text instead).
async function renderMermaidDiagram(container, diagramText) {
  if (!window.mermaid) {
    console.warn('mermaid not loaded');
    return false;
  }
  ensureMermaidInitialized();
  // Belt-and-braces: even with suppressErrorRendering set, don't trust a
  // CDN-pinned "@10" (a moving minor/patch version) to honor it forever.
  // Snapshot document.body's children so a failed render's own DOM debris
  // -- wherever mermaid decided to put it -- can be swept up regardless of
  // its id/class naming, which varies by version.
  const before = new Set(document.body.children);
  try {
    const { svg } = await window.mermaid.render('mermaid-' + Date.now(), diagramText);
    container.innerHTML = svg;
    return true;
  } catch (err) {
    console.warn('Failed to render mermaid diagram:', err);
    for (const node of Array.from(document.body.children)) {
      if (!before.has(node)) node.remove();
    }
    return false;
  }
}

// --- diff helpers -----------------------------------------------------------

// Fetch a diff between two versions and return the diff lines
async function fetchDiff(artifactId, fromVersionId, toVersionId) {
  try {
    const res = await fetch(
      `/api/diff/${artifactId}?from=${fromVersionId}&to=${toVersionId}`
    );
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      console.error('Diff fetch failed:', error.error || res.statusText);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error('Diff fetch error:', err);
    return null;
  }
}

// Render a diff into a container
function renderDiff(container, diffData) {
  if (!diffData || !diffData.lines || !Array.isArray(diffData.lines)) {
    container.textContent = "No changes between versions";
    return;
  }

  container.innerHTML = "";
  const diffBox = el("div", "diff-container");

  diffData.lines.forEach((line) => {
    const lineEl = el("span", "diff-line");

    // Classify the line
    if (line.startsWith("---") || line.startsWith("+++") || line.startsWith("@@")) {
      lineEl.classList.add("header");
    } else if (line.startsWith("+")) {
      lineEl.classList.add("added");
    } else if (line.startsWith("-")) {
      lineEl.classList.add("removed");
    } else {
      lineEl.classList.add("context");
    }

    // Remove trailing newline for display (it's part of the line from difflib)
    const displayLine = line.endsWith("\n") ? line.slice(0, -1) : line;
    lineEl.textContent = displayLine;
    diffBox.appendChild(lineEl);
  });

  container.appendChild(diffBox);
}

// --- dependency graph helpers ------------------------------------------------

// Fetch the Mermaid text for an artifact's immediate upstream/downstream
// neighborhood. Returns null on any failure so the caller can show a plain
// error message instead of a broken diagram.
async function fetchGraph(artifactId) {
  try {
    const res = await fetch(`/api/graph/${artifactId}`);
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      console.error('Graph fetch failed:', error.error || res.statusText);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error('Graph fetch error:', err);
    return null;
  }
}

// Render a dependency graph into a container, reusing T1's Mermaid render
// path so there is exactly one place that talks to mermaid.js.
async function renderGraph(container, graphData) {
  container.innerHTML = "";
  if (!graphData || !graphData.mermaid) {
    container.textContent = "Failed to load dependency graph";
    return;
  }
  const diagramContainer = el("div", "mermaid-diagram");
  container.appendChild(diagramContainer);
  const success = await renderMermaidDiagram(diagramContainer, graphData.mermaid);
  if (!success) {
    diagramContainer.className = "mermaid-diagram-error";
    diagramContainer.textContent = "```mermaid\n" + graphData.mermaid + "\n```";
    return;
  }
  if (!graphData.has_dependencies) {
    container.appendChild(el("p", "hint", "No dependencies (no upstream or downstream artifacts)."));
  }
}

// --- in-flight -------------------------------------------------------------

function paintInflight() {
  const on = inflight > 0;
  $("progress").classList.toggle("on", on);
  document.querySelectorAll("button, input, textarea, select").forEach((node) => {
    if (node.dataset.keepEnabled === "1") return;
    // A control disabled for its own reason (an approve blocked by a lock)
    // must not come back enabled when the in-flight phase ends.
    if (node.dataset.keepDisabled === "1") { node.disabled = true; return; }
    node.disabled = on;
  });
  document.querySelectorAll(".draft").forEach((node) => {
    node.classList.toggle("dim", on);
  });
  paintHeaderState();
}

function paintHeaderState() {
  const dot = $("dot");
  const label = $("statelabel");
  if (inflight > 0) {
    const secs = Math.max(1, Math.round((Date.now() - inflightSince) / 1000));
    dot.className = "dot working";
    label.textContent = secs >= 20 ? `still working ${secs}s` : `sending ${secs}s`;
    return;
  }
  if (!state) {
    dot.className = "dot idle";
    label.textContent = "connecting";
    return;
  }
  const queued = state.pending_event_count || 0;
  const working = state.processing_event_count || 0;
  dot.className = "dot " + (working ? "working" : queued ? "queued" : "idle");
  // status_message is computed server-side from the events table, so the
  // header can never claim "Ready" while the inbox holds unclaimed work.
  const review = state.pending_review_count || 0;
  const bits = [state.status_message || "Ready"];
  if (review) bits.push(`${review} awaiting review`);
  label.textContent = bits.join(" · ");
}

function startInflight() {
  inflight += 1;
  if (inflight === 1) {
    inflightSince = Date.now();
    tickTimer = setInterval(paintHeaderState, 1000);
  }
  paintInflight();
}

function endInflight() {
  inflight = Math.max(0, inflight - 1);
  if (inflight === 0 && tickTimer) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
  paintInflight();
}

// --- server calls ----------------------------------------------------------

async function act(payload) {
  startInflight();
  const request = Object.assign({}, payload);
  delete request.clearSlot;              // client-only bookkeeping
  try {
    const res = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!res.ok) {
      // A 401 (session/token changed mid-use) or a 400/500 (malformed
      // request, server error) carries no `state` to render and, before
      // this check, `{}.ok === false` was false too -- so this branch used
      // to be silently treated as SUCCESS: the notice was cleared and the
      // user's typed text was discarded with no feedback at all.
      let detail = res.statusText;
      try { detail = (await res.json()).error || detail; } catch (_) { /* no body */ }
      notice = { text: `Request failed (${res.status}): ${detail}`, ok: false };
      render(state, true);
      return;
    }
    const body = await res.json();
    const result = body.result || {};
    if (result.ok === false) {
      notice = { text: result.message || result.error || "Action failed", ok: false };
    } else {
      notice = null;
      // The action has been accepted; drop the composer text it came from so
      // the field does not silently re-submit on the next click.
      if (payload.clearSlot) delete drafts[draftKey(payload.artifact_id, payload.clearSlot)];
    }
    render(body.state, true);
  } catch (err) {
    notice = { text: "Could not reach the board server: " + err, ok: false };
    render(state, true);
  } finally {
    endInflight();
  }
}

async function poll() {
  if (inflight > 0) return;              // never repaint over an open POST
  try {
    const res = await fetch("/api/state");
    if (!res.ok) return;
    render(await res.json(), false);
  } catch (err) {
    /* transient: the next tick retries */
  }
}

// --- card pieces -----------------------------------------------------------

function metaLine(card) {
  const box = el("p", "card-meta");
  const bits = [];
  if (card.selected_version) {
    bits.push(`v${card.selected_version.version_num} of ${card.all_versions.length}`);
  } else if (card.all_versions.length) {
    bits.push(`${card.all_versions.length} versions`);
  }
  bits.push(card.stage);
  if (card.locked) bits.push("locked");
  if (card.has_generating_job) bits.push("generating");
  if (card.updated_age) bits.push(`updated ${card.updated_age} ago`);
  bits.forEach((b, i) => {
    if (i) box.appendChild(el("span", "sep", "·"));
    box.appendChild(el("span", null, b));
  });
  return box;
}

function queuedBanner(card) {
  if (!card.queued_note) return null;
  const working = (card.processing_event_count || 0) > 0;
  const box = el("div", "queued-note" + (working ? " working" : ""));
  box.dataset.artifact = card.artifact_id;
  box.appendChild(el("span", "dot"));
  box.appendChild(el("span", "qtext", card.queued_note));
  return box;
}

// The queued note carries an age ("queued 40s ago"), and the board's
// fingerprint does not change while an event just sits there — so a
// fingerprint-gated repaint would freeze the age at whatever it was when the
// card was drawn. This runs on EVERY poll, whether or not the board changed,
// and rewrites only the text nodes that carry live values. It never rebuilds
// DOM, so it cannot disturb a half-typed composer or an open <details>.
function paintLiveValues(next) {
  const notes = {};
  (next.stages || []).forEach((s) => {
    (s.artifacts || []).forEach((a) => { notes[a.artifact_id] = a.queued_note; });
  });
  document.querySelectorAll(".queued-note").forEach((box) => {
    const text = notes[box.dataset.artifact];
    const span = box.querySelector(".qtext");
    if (span && text && span.textContent !== text) span.textContent = text;
  });
}

function contentBlock(card) {
  if (card.content_kind === "text") {
    const box = el("div", "draft");
    if (!card.content_text) {
      box.classList.add("empty");
      box.textContent = "(this version has no text content)";
    } else {
      if (card.selected_version && card.selected_version.content_type === "application/json") {
        box.classList.add("json");
      }

      // Check if content has mermaid blocks
      const blocks = extractMermaidBlocks(card.content_text);
      const hasMermaid = blocks.some(b => b.type === 'mermaid');

      if (hasMermaid) {
        // Build the content with mermaid diagrams
        blocks.forEach((block, idx) => {
          if (block.type === 'text') {
            // Add text content as a text node
            if (block.content) {
              box.appendChild(document.createTextNode(block.content));
            }
          } else {
            // Create a container for the mermaid diagram
            const diagramContainer = el("div", "mermaid-diagram");
            box.appendChild(diagramContainer);

            // Render the diagram asynchronously
            renderMermaidDiagram(diagramContainer, block.content).then(success => {
              if (!success) {
                // Fallback: show the raw fenced block as text
                diagramContainer.className = "mermaid-diagram-error";
                diagramContainer.textContent = "```mermaid\n" + block.content + "\n```";
              }
            }).catch(err => {
              // Double fallback for any uncaught errors
              diagramContainer.className = "mermaid-diagram-error";
              diagramContainer.textContent = "```mermaid\n" + block.content + "\n```";
              console.error("Mermaid rendering error:", err);
            });
          }
        });
      } else {
        // No mermaid blocks, render as plain text
        box.textContent = card.content_text;   // text node: newlines survive
      }
    }
    return box;
  }

  if (["image", "video", "audio"].indexOf(card.content_kind) >= 0) {
    const box = el("div", "media");
    if (!card.media_url) {
      box.appendChild(el("div", "media-missing", "No media file for this version"));
      return box;
    }
    let node;
    if (card.content_kind === "image") {
      node = el("img");
      node.alt = card.title;
      node.loading = "lazy";
    } else {
      node = el(card.content_kind === "video" ? "video" : "audio");
      node.controls = true;
    }
    node.src = card.media_url;
    // A missing/unreadable file must degrade to a label, not a broken glyph.
    node.onerror = () => {
      box.innerHTML = "";
      box.appendChild(el("div", "media-missing", card.media_ref || "media unavailable"));
    };
    box.appendChild(node);
    return box;
  }

  const box = el("div", "draft empty");
  box.textContent = card.selected_version
    ? `(${card.content_kind} content is not rendered inline)`
    : "(no version selected yet)";
  return box;
}

function promptFold(card) {
  const v = card.selected_version;
  if (!v || (!v.prompt && !v.note)) return null;
  const fold = el("details", "fold");
  fold.appendChild(el("summary", null, "Prompt & notes"));
  const body = el("div", "fold-body");
  if (v.prompt) {
    const p = el("p");
    p.appendChild(el("b", null, "Prompt "));
    p.appendChild(document.createTextNode(v.prompt));
    body.appendChild(p);
  }
  if (v.note) {
    const p = el("p");
    p.appendChild(el("b", null, "Note "));
    p.appendChild(document.createTextNode(v.note));
    body.appendChild(p);
  }
  fold.appendChild(body);
  return fold;
}

function historyFold(card) {
  if (card.all_versions.length < 2) return null;
  const canSelect = card.allowed_actions.indexOf("select_version") >= 0;
  const fold = el("details", "fold");
  fold.appendChild(el("summary", null, `Version history (${card.all_versions.length})`));
  const body = el("div");
  const current = card.selected_version ? card.selected_version.version_id : "";

  [...card.all_versions].reverse().forEach((v, idx) => {
    const row = el("div", "vrow" + (v.is_selected ? " current" : ""));
    const left = el("span", "vlabel");
    left.appendChild(el("b", null, "v" + v.version_num));
    const detail = [v.created_by, v.age ? v.age + " ago" : "", v.note || ""]
      .filter(Boolean).join(" · ");
    if (detail) left.appendChild(document.createTextNode(" " + detail));
    row.appendChild(left);

    if (v.is_selected) {
      row.appendChild(el("span", null, "selected"));
    } else if (canSelect) {
      const b = el("button", null, "Select");
      b.onclick = () => act({
        action: "select_version",
        artifact_id: card.artifact_id,
        version_id: v.version_id,
        // Optimistic concurrency: if someone else selected a different
        // version since this page painted, the store reports a conflict
        // instead of silently overwriting them (SPEC section 41).
        expected_selected_version_id: current,
      });
      row.appendChild(b);
    }
    body.appendChild(row);
  });

  // Add compare button if selected version exists and there's a previous version
  if (card.selected_version && card.all_versions.length >= 2) {
    const selectedIdx = card.all_versions.findIndex(v => v.is_selected);
    if (selectedIdx > 0) {
      const prevVersion = card.all_versions[selectedIdx - 1];
      const compareRow = el("div", "vrow");
      const compareBtn = el("button", null, "Compare to previous version");
      compareBtn.onclick = async () => {
        const diffContainer = $("diff-container-" + card.artifact_id);
        if (diffContainer) {
          // Toggle visibility
          if (diffContainer.style.display === "none") {
            diffContainer.style.display = "block";
            // Fetch and render diff if empty
            if (!diffContainer.children.length) {
              const loadingMsg = el("p");
              loadingMsg.style.color = "var(--fg-muted)";
              loadingMsg.textContent = "Loading diff...";
              diffContainer.appendChild(loadingMsg);

              const diffData = await fetchDiff(
                card.artifact_id,
                prevVersion.version_id,
                card.selected_version.version_id
              );
              diffContainer.innerHTML = "";
              if (diffData) {
                renderDiff(diffContainer, diffData);
              } else {
                const errMsg = el("p");
                errMsg.style.color = "var(--fg-muted)";
                errMsg.textContent = "Failed to load diff";
                diffContainer.appendChild(errMsg);
              }
            }
          } else {
            diffContainer.style.display = "none";
          }
        }
      };
      compareRow.appendChild(compareBtn);
      body.appendChild(compareRow);
    }
  }

  fold.appendChild(body);
  return fold;
}

function formFields(card) {
  const box = el("div", "controls");
  const inputs = [];
  card.form_fields.forEach((f) => {
    const field = el("div", "field");
    // A schema field's title is a full question ("What is the main goal?"),
    // so it reads as a sentence, not as an uppercase micro-label.
    const label = el("div", "label question");
    label.appendChild(document.createTextNode(f.label));
    if (f.required) label.appendChild(el("span", "req", " *"));
    field.appendChild(label);
    if (f.description) field.appendChild(el("div", "hint", f.description));

    let input;
    if (f.enum && f.enum.length) {
      input = el("select");
      const unanswered = f.value === null || f.value === undefined || f.value === "";
      if (unanswered) {
        // Without this the select silently shows the first option as if it
        // had been chosen. An unanswered question must look unanswered.
        const blank = el("option", null, "— choose —");
        blank.value = "";
        input.appendChild(blank);
      }
      f.enum.forEach((opt) => {
        const o = el("option", null, String(opt));
        o.value = String(opt);
        input.appendChild(o);
      });
      input.value = f.value === null || f.value === undefined ? "" : String(f.value);
    } else if (f.type === "boolean") {
      const wrap = el("label", "check");
      input = el("input");
      input.type = "checkbox";
      input.checked = !!f.value;
      wrap.appendChild(input);
      wrap.appendChild(el("span", null, "yes"));
      field.appendChild(wrap);
    } else if (f.type === "integer" || f.type === "number") {
      input = el("input");
      input.type = "number";
      input.value = f.value === null || f.value === undefined ? "" : f.value;
    } else {
      input = el("input");
      input.type = "text";
      input.value = f.value === null || f.value === undefined ? "" : String(f.value);
    }
    if (f.type !== "boolean") field.appendChild(input);
    inputs.push({ field: f, input });
    box.appendChild(field);
  });

  if (card.allowed_actions.indexOf("edit") >= 0) {
    const row = el("div", "row");
    const submit = el("button", "primary", "Submit answers");
    submit.onclick = () => {
      const answers = {};
      inputs.forEach(({ field, input }) => {
        if (field.type === "boolean") answers[field.name] = input.checked;
        else if (field.type === "integer" || field.type === "number") {
          answers[field.name] = input.value === "" ? null : Number(input.value);
        } else answers[field.name] = input.value;
      });
      act({
        action: "edit",
        artifact_id: card.artifact_id,
        content: JSON.stringify(answers),
      });
    };
    row.appendChild(submit);
    box.appendChild(row);
  }
  return box;
}

function composer(card, slot, opts) {
  const box = el("div", "field");
  box.appendChild(el("div", "label", opts.label));
  const row = el("div", "row stretch");
  const input = opts.multiline ? el("textarea") : el("input");
  if (!opts.multiline) input.type = "text";
  input.placeholder = opts.placeholder;
  rememberInput(input, card.artifact_id, slot);
  const send = el("button", opts.primary ? "primary" : null, opts.button);
  send.onclick = () => {
    if (!input.value.trim()) return;
    const payload = { action: opts.action, artifact_id: card.artifact_id, clearSlot: slot };
    payload[opts.fieldName] = input.value;
    act(payload);
  };
  if (!opts.multiline) {
    input.onkeydown = (e) => { if (e.key === "Enter") send.onclick(); };
  }
  row.appendChild(input);
  row.appendChild(send);
  box.appendChild(row);
  return box;
}

function actionRow(card) {
  const row = el("div", "row");
  const allowed = card.allowed_actions;
  let any = false;

  if (allowed.indexOf("approve") >= 0) {
    const b = el("button", "primary", card.locked ? "Approve (locked)" : "Approve");
    b.disabled = card.locked;
    if (card.locked) {
      b.dataset.keepDisabled = "1";
      b.title = "Unlock this artifact before approving it";
    }
    b.onclick = () => act({ action: "approve", artifact_id: card.artifact_id });
    row.appendChild(b); any = true;
  }
  if (allowed.indexOf("regenerate") >= 0) {
    const b = el("button", null, "Regenerate");
    b.onclick = () => act({ action: "regenerate", artifact_id: card.artifact_id });
    row.appendChild(b); any = true;
  }
  if (allowed.indexOf("reopen") >= 0) {
    const b = el("button", null, "Reopen");
    b.onclick = () => act({ action: "reopen", artifact_id: card.artifact_id });
    row.appendChild(b); any = true;
  }
  if (allowed.indexOf("lock") >= 0 || allowed.indexOf("unlock") >= 0) {
    const want = card.locked ? "unlock" : "lock";
    if (allowed.indexOf(want) >= 0) {
      const b = el("button", null, card.locked ? "Unlock" : "Lock");
      b.onclick = () => act({ action: want, artifact_id: card.artifact_id });
      row.appendChild(b); any = true;
    }
  }
  if (allowed.indexOf("cancel") >= 0) {
    const b = el("button", "danger", "Cancel");
    b.onclick = () => act({ action: "cancel", artifact_id: card.artifact_id });
    row.appendChild(b); any = true;
  }
  return any ? row : null;
}

function buildCard(card, minimal = false) {
  const box = el("article", "card" + (card.status === "approved" ? " is-approved" : "") + (card.busy ? " is-busy" : ""));

  const head = el("div", "card-head");
  head.appendChild(el("h2", "card-title", card.title));
  head.appendChild(el("span", "chip-status st-" + card.status, card.status));
  box.appendChild(head);

  if (!minimal) {
    box.appendChild(metaLine(card));

    const banner = queuedBanner(card);
    if (banner) box.appendChild(banner);

    if (card.content_kind === "form") {
      box.appendChild(formFields(card));
    } else {
      box.appendChild(contentBlock(card));

      // Add a container for diff display (populated when "Compare to previous version" is clicked)
      const diffContainer = el("div");
      diffContainer.id = "diff-container-" + card.artifact_id;
      diffContainer.style.display = "none";
      box.appendChild(diffContainer);
    }

    // Dependencies: renders the artifact's immediate upstream/downstream
    // neighborhood as a Mermaid graph, through T1's shared render path.
    const depsRow = el("div", "row");
    const depsBtn = el("button", null, "Dependencies");
    const depsContainer = el("div");
    depsContainer.id = "graph-container-" + card.artifact_id;
    depsContainer.style.display = "none";
    depsBtn.onclick = async () => {
      if (depsContainer.style.display === "none") {
        depsContainer.style.display = "block";
        depsContainer.innerHTML = "";
        const loadingMsg = el("p");
        loadingMsg.style.color = "var(--fg-muted)";
        loadingMsg.textContent = "Loading dependency graph...";
        depsContainer.appendChild(loadingMsg);
        const graphData = await fetchGraph(card.artifact_id);
        await renderGraph(depsContainer, graphData);
      } else {
        depsContainer.style.display = "none";
      }
    };
    depsRow.appendChild(depsBtn);
    box.appendChild(depsRow);
    box.appendChild(depsContainer);

    const prompt = promptFold(card);
    if (prompt) box.appendChild(prompt);
    const history = historyFold(card);
    if (history) box.appendChild(history);

    const controls = el("div", "controls");
    let hasControls = false;

    if (card.allowed_actions.indexOf("edit") >= 0 && card.content_kind !== "form") {
      controls.appendChild(composer(card, "edit", {
        label: "Replace content",
        placeholder: "New content for this artifact…",
        button: "Update",
        action: "edit",
        fieldName: "content",
        multiline: true,
      }));
      hasControls = true;
    }
    if (card.allowed_actions.indexOf("revise") >= 0) {
      controls.appendChild(composer(card, "revise", {
        label: "Ask for a revision",
        placeholder: "What should change?",
        button: "Revise",
        action: "revise",
        fieldName: "note",
      }));
      hasControls = true;
    }
    if (card.allowed_actions.indexOf("message") >= 0) {
      // Bound to THIS artifact. The old board's footer box posted
      // artifact_id: null, which reached nothing in particular.
      controls.appendChild(composer(card, "message", {
        label: "Message the worker about this artifact",
        placeholder: "Note for the worker…",
        button: "Send",
        action: "message",
        fieldName: "text",
      }));
      hasControls = true;
    }
    const actions = actionRow(card);
    if (actions) { controls.appendChild(actions); hasControls = true; }
    if (hasControls) box.appendChild(controls);
  } else {
    // Minimal card for column view: only show metadata and actions for "review" status
    const meta = el("p", "card-meta");
    if (card.updated_age) meta.appendChild(el("span", null, `updated ${card.updated_age} ago`));
    box.appendChild(meta);

    const banner = queuedBanner(card);
    if (banner) box.appendChild(banner);

    // Only show approve/reopen buttons for tasks
    if (card.allowed_actions.indexOf("approve") >= 0 || card.allowed_actions.indexOf("reopen") >= 0) {
      const controls = el("div", "controls");
      const actions = actionRow(card);
      if (actions) controls.appendChild(actions);
      if (controls.children.length > 0) box.appendChild(controls);
    }
  }

  return box;
}

// Build column layout for column view
function buildColumnView(stage) {
  const container = el("div", "columns-container");

  // Status to column label mapping
  const statusColumns = {
    draft: "Planned",
    generating: "In Progress",
    review: "Needs Approval",
    approved: "Done",
    failed: "Failed",
    cancelled: "Cancelled"
  };

  // Create columns for each status
  const columns = {};
  Object.entries(statusColumns).forEach(([status, label]) => {
    const col = el("div", "column");
    col.dataset.status = status;
    const header = el("div", "column-header");
    header.appendChild(el("h3", "column-title", label));
    const count = stage.artifacts.filter(a => a.status === status).length;
    header.appendChild(el("span", "column-count", String(count)));
    col.appendChild(header);
    columns[status] = col;
    container.appendChild(col);
  });

  // Place cards into columns
  stage.artifacts.forEach((card) => {
    const status = card.status || "draft";
    if (columns[status]) {
      columns[status].appendChild(buildCard(card, true));
    }
  });

  return container;
}

// --- top level -------------------------------------------------------------

function drawTabs() {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  const stages = state.stages || [];
  tabs.hidden = stages.length < 2;
  if (stages.length < 2) return;

  stages.forEach((s) => {
    const b = el("button", "tab" + (s.stage_id === activeStage ? " on" : ""));
    b.appendChild(document.createTextNode(s.stage_title));
    if (s.total_count) {
      b.appendChild(el("span", "count", `${s.approved_count}/${s.total_count}`));
    }
    if (s.queued_count) b.appendChild(el("span", "qmark", "●"));
    b.dataset.keepEnabled = "1";   // navigation stays usable during a POST
    b.onclick = () => { activeStage = s.stage_id; render(state, true); };
    tabs.appendChild(b);
  });
}

function render(next, force) {
  if (!next) return;
  state = next;
  $("brand").textContent = "Agent Surface · " + (state.title || "board");
  document.title = (state.title || "Agent Surface") + " — Board";

  const stages = state.stages || [];
  if (!activeStage || !stages.some((s) => s.stage_id === activeStage)) {
    activeStage = stages.length ? stages[0].stage_id : null;
  }

  // Repaint only when something actually changed, so a 2s poll does not
  // wipe half-typed text or reset an open <details> on every tick.
  if (!force && state.fingerprint === lastFingerprint) {
    paintHeaderState();
    paintLiveValues(state);
    return;
  }
  lastFingerprint = state.fingerprint;

  drawTabs();

  const noticeEl = $("notice");
  noticeEl.hidden = !notice;
  if (notice) {
    noticeEl.textContent = notice.text;
    noticeEl.className = "notice" + (notice.ok ? " ok" : "");
  }

  const cards = $("cards");
  cards.innerHTML = "";
  const stage = stages.find((s) => s.stage_id === activeStage);
  if (!stage || !stage.artifacts.length) {
    cards.appendChild(el("p", "empty-stage", "No artifacts in this stage yet."));
  } else {
    // Check if this stage uses column view
    if (stage.board_view === "columns") {
      cards.appendChild(buildColumnView(stage));
    } else {
      stage.artifacts.forEach((card) => cards.appendChild(buildCard(card)));
    }
  }

  paintInflight();
}

poll();
setInterval(poll, 2000);
