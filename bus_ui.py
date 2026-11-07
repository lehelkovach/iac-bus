"""The bus status page, served at GET /ui.

One static page (no build step, no external assets) that shows locks, runs,
pending decisions, status reports and lock events, and lets a human answer a
decision with Approve or Deny. It is served without a token; the page asks for
the bus token once and keeps it in the browser's localStorage, and every API
call it makes carries that token. Kept in a Python module so it ships inside
the installed package.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iac-bus</title>
<style>
  :root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#ddd; --ok:#1b7f3b; --warn:#b26a00; --bad:#b3261e; --card:#f6f6f6; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#eee; --muted:#aaa; --line:#333; --card:#1c1c1c; } }
  body { margin:0; padding:16px; font:14px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  h1 { font-size:18px; margin:0 0 8px; } h2 { font-size:15px; margin:18px 0 6px; }
  table { border-collapse:collapse; width:100%; } th, td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; } .muted { color:var(--muted); } .mono { font-family:ui-monospace, monospace; font-size:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0; }
  button { font:inherit; padding:5px 10px; border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer; }
  button.ok { border-color:var(--ok); color:var(--ok); } button.bad { border-color:var(--bad); color:var(--bad); }
  input, select { font:inherit; padding:5px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:12px; border:1px solid var(--line); }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
  #err { color:var(--bad); min-height:1.2em; }
</style>
</head>
<body>
<h1>iac-bus <span id="health" class="muted"></span></h1>
<div class="row">
  <input id="token" type="password" placeholder="bus token (shared or run token)" size="40">
  <button id="save">Use token</button>
  <label class="muted"><input type="checkbox" id="auto" checked> refresh every 5 s</label>
  <span id="err"></span>
</div>

<h2>Pending decisions</h2>
<div id="decisions"><span class="muted">none</span></div>

<h2>Locks</h2>
<table><thead><tr><th>repo</th><th>path</th><th>mode</th><th>held by</th><th>reason</th><th>expires in</th></tr></thead><tbody id="locks"></tbody></table>

<h2>Runs</h2>
<table><thead><tr><th>actor</th><th>role</th><th>status</th><th>last seen</th><th>expires in</th></tr></thead><tbody id="runs"></tbody></table>

<h2>Status reports</h2>
<table><thead><tr><th>when</th><th>agent</th><th>state</th><th>summary</th></tr></thead><tbody id="status"></tbody></table>

<h2>Lock events</h2>
<table><thead><tr><th>when</th><th>event</th><th>path</th><th>agent</th></tr></thead><tbody id="events"></tbody></table>

<script>
const $ = id => document.getElementById(id);
const token = () => localStorage.getItem("iacBusToken") || "";
$("token").value = token();
$("save").onclick = () => { localStorage.setItem("iacBusToken", $("token").value.trim()); refresh(); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ago = ts => { const d = Math.max(0, Date.now()/1000 - ts); return d < 60 ? Math.round(d)+"s ago" : d < 3600 ? Math.round(d/60)+"m ago" : Math.round(d/3600)+"h ago"; };
const left = ts => { const d = ts - Date.now()/1000; return d <= 0 ? "expired" : d < 90 ? Math.round(d)+"s" : Math.round(d/60)+"m"; };
const when = ts => new Date(ts*1000).toLocaleTimeString();

async function api(path, body) {
  const headers = {"Content-Type": "application/json"};
  if (token()) headers["Authorization"] = "Bearer " + token();
  const r = await fetch(path, {method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined});
  if (!r.ok) throw new Error(path + " -> " + r.status + (r.status === 401 ? " (set the token)" : ""));
  return r.json();
}

function decisionCards(msgs) {
  const answered = new Set(msgs.filter(m => m.type === "provide_decision").map(m => m.conversation_id || (m.message && m.message.conversation_id)));
  const open = msgs.filter(m => m.type === "request_decision" && !answered.has(m.conversation_id || (m.message && m.message.conversation_id)));
  if (!open.length) { $("decisions").innerHTML = '<span class="muted">none</span>'; return; }
  $("decisions").innerHTML = open.map(m => {
    const env = (m.message && typeof m.message === "object") ? m.message : {};
    const p = env.payload || {};
    const conv = m.conversation_id || env.conversation_id || "";
    const choices = (p.choices || []).map(c => `<option value="${esc(c.id)}">${esc(c.label || c.id)}</option>`).join("");
    return `<div class="card" data-conv="${esc(conv)}" data-channel="${esc(m.channel)}" data-to="${esc(m.sender)}">
      <div><b>${esc(m.sender)}</b> <span class="muted">asks, ${ago(m.ts)}</span></div>
      <div style="margin:6px 0; white-space:pre-wrap">${esc(p.prompt || JSON.stringify(m.message))}</div>
      <div class="row">${choices ? `<select class="choice"><option value="">(choice)</option>${choices}</select>` : ""}
        <input class="notes" placeholder="notes for the agent" size="36">
        <button class="ok" onclick="answer(this, true)">Approve</button>
        <button class="bad" onclick="answer(this, false)">Deny</button></div>
    </div>`;
  }).join("");
}

async function answer(btn, approved) {
  const card = btn.closest(".card");
  const choice = card.querySelector(".choice"); const notes = card.querySelector(".notes").value;
  const payload = {approved};
  if (choice && choice.value) payload.choice_id = choice.value;
  if (notes) payload.notes = notes;
  payload.decision = approved ? "approve" : "deny";
  try {
    await api("/bus/messages", {channel: card.dataset.channel || "decisions", type: "provide_decision", sender: "ui",
      conversation_id: card.dataset.conv, recipient: card.dataset.to,
      message: {kind: "response", action: "provide_decision", from: "ui", to: card.dataset.to, conversation_id: card.dataset.conv, payload}});
    refresh();
  } catch (e) { $("err").textContent = e.message; }
}

async function refresh() {
  $("err").textContent = "";
  try {
    const h = await api("/health");
    $("health").textContent = `${h.version || ""} · ${h.locks_held} locks · ${h.runs_active} runs · ${h.queue_pending_count} queued · ${h.a2a_tasks} a2a tasks`;
  } catch (e) { $("err").textContent = e.message; }
  if (!token()) { $("err").textContent = "enter the bus token to load locks, runs and decisions"; return; }
  try {
    const [locks, runs, decisions, status, events] = await Promise.all([
      api("/bus/locks"), api("/agents/runs"),
      api("/bus/messages?channel=decisions&limit=200"), api("/bus/messages?channel=status&limit=50"),
      api("/bus/messages?channel=locks&limit=50")]);
    $("locks").innerHTML = locks.locks.map(l => `<tr><td>${esc(l.repo)}</td><td class="mono">${esc(l.resource)}</td><td>${esc(l.mode)}</td><td>${esc(l.agent)}</td><td class="muted">${esc(l.reason)}</td><td>${left(l.expires_at)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">none</td></tr>';
    $("runs").innerHTML = runs.runs.map(r => `<tr><td>${esc(r.actor)}</td><td>${esc(r.role)}</td><td>${esc(r.status)}</td><td>${ago(r.last_seen_at)}</td><td>${left(r.expires_at)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">none</td></tr>';
    decisionCards(decisions.messages);
    $("status").innerHTML = status.messages.slice().reverse().map(m => { const p = (m.message && m.message.payload) || {}; const st = p.state || (m.message && m.message.state) || "";
      const cls = st === "failed" ? "bad" : st === "completed" ? "ok" : st === "waiting_input" ? "warn" : "";
      return `<tr><td>${when(m.ts)}</td><td>${esc(m.sender)}</td><td class="${cls}">${esc(st)}</td><td>${esc(p.summary || "")}</td></tr>`; }).join("") || '<tr><td colspan="4" class="muted">none</td></tr>';
    $("events").innerHTML = events.messages.slice().reverse().map(m => `<tr><td>${when(m.ts)}</td><td>${esc(m.type)}</td><td class="mono">${esc(m.message.resource)}</td><td>${esc(m.message.agent)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">none</td></tr>';
  } catch (e) { $("err").textContent = e.message; }
}
refresh();
setInterval(() => { if ($("auto").checked) refresh(); }, 5000);
</script>
</body>
</html>
"""
