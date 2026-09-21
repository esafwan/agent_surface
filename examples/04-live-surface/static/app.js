// Renders whatever the agent declared. The server is the source of truth:
// a reload mid-turn repaints real state instead of orphaning the loop.

const $ = (id) => document.getElementById(id);
let state = null;
let viewing = null;      // version number being previewed, or null for current
let timer = null;

function setInflight(on, sentText) {
  // The ask stays on screen during the call: replacing it with a spinner
  // means losing the question you are in the middle of answering.
  $("progress").classList.toggle("on", on);
  $("draft").classList.toggle("dim", on);
  $("dot").className = "dot " + (on ? "writing" : (state?.done ? "approved" : "idle"));
  $("statelabel").textContent = on ? "writing" : (state?.done ? "approved" : "idle");
  document.querySelectorAll(".dock button, .dock input")
    .forEach((el) => { el.disabled = on; });

  if (on) {
    let s = 1;
    $("statelabel").textContent = "writing 1s";
    timer = setInterval(() => {
      s += 1;
      $("statelabel").textContent = s >= 20 ? `still working… ${s}s` : `writing ${s}s`;
    }, 1000);
  } else if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

// Every agent string reaches the DOM as a text node, never innerHTML:
// keeps newlines, and a poem containing <script> renders as characters.
function buildBlock(b) {
  if (b.type === "table") {
    const t = document.createElement("table");
    const head = document.createElement("tr");
    (b.columns || []).forEach((c) => {
      const th = document.createElement("th");
      th.textContent = c;
      head.appendChild(th);
    });
    t.appendChild(head);
    (b.rows || []).forEach((row) => {
      const tr = document.createElement("tr");
      row.forEach((cell) => {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      });
      t.appendChild(tr);
    });
    return t;
  }

  if (b.type === "thread") {
    const wrap = document.createElement("div");
    wrap.className = "thread";
    (b.turns || []).forEach((t) => {
      const turn = document.createElement("div");
      turn.className = "turn " + (t.role === "user" ? "user" : "agent");
      const who = document.createElement("div");
      who.className = "who";
      who.textContent = t.role === "user" ? "you" : "agent";
      const body = document.createElement("div");
      body.className = "bubble";
      body.textContent = t.text;
      turn.appendChild(who);
      turn.appendChild(body);
      wrap.appendChild(turn);
    });
    return wrap;
  }

  const el = document.createElement("div");
  el.className = "block " + (b.type || "prose");
  el.textContent = b.text || "";
  return el;
}

function drawBlocks(blocks, isApproved) {
  const el = $("draft");
  el.innerHTML = "";
  el.className = "draft";
  if (!blocks || blocks.length === 0) {
    el.classList.add("empty");
    el.textContent = "(nothing yet)";
    return;
  }
  blocks.forEach((b) => el.appendChild(buildBlock(b)));
  el.classList.toggle("approved", !!isApproved && !viewing);
  el.classList.toggle("stale", viewing !== null);
}

function drawHistory() {
  const versions = state.versions || [];
  const h = $("history");
  if (versions.length < 2) { h.hidden = true; return; }
  h.hidden = false;
  h.open = !!state.done;
  $("histsummary").textContent =
    `${versions.length} drafts`;
  const rows = $("histrows");
  rows.innerHTML = "";
  [...versions].reverse().forEach((v) => {
    const b = document.createElement("button");
    b.className = "vrow";
    const isCurrent = v.n === versions.length;
    b.innerHTML = "";
    const strong = document.createElement("b");
    strong.textContent = "v" + v.n;
    b.appendChild(strong);
    const note = document.createElement("span");
    note.textContent = isCurrent ? "current" : (v.note ? `"${v.note}"` : "");
    b.appendChild(note);
    b.onclick = () => (isCurrent ? backToCurrent() : showVersion(v.n));
    rows.appendChild(b);
  });
}

async function showVersion(n) {
  const r = await fetch(`/version/${n}`);
  if (!r.ok) return;
  const v = await r.json();
  viewing = n;
  drawBlocks([{ type: "verse", text: v.draft }], false);
  $("stalelabel").textContent = `Viewing v${n} — not current`;
  $("stalebar").classList.add("on");
  document.querySelectorAll(".dock button, .dock input")
    .forEach((el) => { el.disabled = true; });
}

function backToCurrent() {
  viewing = null;
  $("stalebar").classList.remove("on");
  render(state);
}

function submit(values) {
  if (!values || Object.keys(values).length === 0) return;
  setInflight(true);
  fetch("/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values, handle: state?.handle }),
  })
    .then((r) => r.json())
    .then((s) => { setInflight(false); render(s); })
    .catch(() => fetch("/state").then((r) => r.json())
      .then((s) => { setInflight(false); render(s); }));
}

function buildControl(c) {
  const box = document.createElement("div");

  if (c.label) {
    const l = document.createElement("div");
    l.className = "label";
    l.textContent = c.label;
    box.appendChild(l);
  }

  if (c.type === "buttons") {
    const row = document.createElement("div");
    row.className = "row";
    c.options.forEach((opt) => {
      const b = document.createElement("button");
      b.textContent = opt;
      if (opt === c.primary) b.className = "primary";
      b.onclick = () => submit({ [c.id]: opt });
      row.appendChild(b);
    });
    box.appendChild(row);
    return box;
  }

  if (c.type === "choice") {
    const row = document.createElement("div");
    row.className = "row";
    c.options.forEach((opt) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = opt;
      b.onclick = () => submit({ [c.id]: opt });
      row.appendChild(b);
    });
    box.appendChild(row);
    return box;
  }

  if (c.type === "multi") {
    const row = document.createElement("div");
    row.className = "row";
    const picked = new Set();
    c.options.forEach((opt) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.textContent = opt;
      b.onclick = () => {
        picked.has(opt) ? picked.delete(opt) : picked.add(opt);
        b.classList.toggle("on", picked.has(opt));
      };
      row.appendChild(b);
    });
    const send = document.createElement("button");
    send.className = "primary";
    send.textContent = "Send";
    send.onclick = () => submit({ [c.id]: [...picked] });
    row.appendChild(send);
    box.appendChild(row);
    return box;
  }

  if (c.type === "table") {
    const t = document.createElement("table");
    const thead = document.createElement("tr");
    c.columns.forEach((col) => {
      const th = document.createElement("th");
      th.textContent = col;
      thead.appendChild(th);
    });
    t.appendChild(thead);
    c.rows.forEach((row) => {
      const tr = document.createElement("tr");
      row.forEach((cell) => {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      });
      t.appendChild(tr);
    });
    box.appendChild(t);
    return box;
  }

  // text (and any unknown type, degraded)
  const row = document.createElement("div");
  row.className = "row";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = c.placeholder || "type your answer…";
  input.onkeydown = (e) => { if (e.key === "Enter") submit({ [c.id]: input.value }); };
  const send = document.createElement("button");
  send.className = "primary";
  send.textContent = "Send";
  send.onclick = () => submit({ [c.id]: input.value });
  row.appendChild(input);
  row.appendChild(send);
  box.appendChild(row);
  return box;
}

function render(s) {
  state = s;
  // Mode drives layout only; all colour/spacing stays in CSS so the agent
  // cannot make the page incoherent.
  document.body.dataset.mode = s.mode || "doc";
  $("ask").textContent = s.ask || "";
  $("ask").classList.toggle("error", !!s.malformed);
  $("ask").hidden = false;

  if (viewing === null) drawBlocks(s.blocks, s.done);
  drawHistory();

  $("meta").hidden = !s.done;
  if (s.done) $("meta").textContent = `✓ approved · ${s.turns} turns`;

  const dock = $("dock");
  const controls = $("controls");
  controls.innerHTML = "";

  if (s.done) {
    const row = document.createElement("div");
    row.className = "row";
    const copy = document.createElement("button");
    copy.textContent = "Copy";
    copy.onclick = () => navigator.clipboard.writeText(s.draft || "");
    copy.title = "Copy the final text";
    const again = document.createElement("button");
    again.className = "primary";
    again.textContent = "Start over";
    again.onclick = () => {
      viewing = null;
      $("stalebar").classList.remove("on");
      fetch("/reset", { method: "POST" }).then((r) => r.json()).then(render);
    };
    row.appendChild(copy);
    row.appendChild(again);
    controls.appendChild(row);
  } else {
    (s.controls || []).forEach((c) => controls.appendChild(buildControl(c)));
  }
  dock.classList.remove("off");

  $("dot").className = "dot " + (s.done ? "approved" : "idle");
  $("statelabel").textContent = s.done ? "approved" : "idle";

  const firstInput = controls.querySelector('input[type="text"]');
  if (firstInput) firstInput.focus();
}

$("backtocurrent").onclick = backToCurrent;

fetch("/state").then((r) => r.json()).then(render);
