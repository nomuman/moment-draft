const $ = (id) => document.getElementById(id);
let session;
let project;
let uploading = false;
let polling = false;
const previews = new Map();
const statuses = new Map();
const waiting = new Map();

function notice(message = "") { $("notice").textContent = message; }
function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
async function api(path, data, method = "POST", headers = {}) {
  const options = { method: data === undefined ? "GET" : method, headers: { ...headers } };
  if (data !== undefined) {
    options.headers["X-CSRF-Token"] = session.csrf;
    if (data instanceof Blob) {
      options.headers["Content-Type"] = "application/octet-stream";
      options.body = data;
    } else {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(data);
    }
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "処理を完了できませんでした。");
  return value;
}
function path(suffix = "") { return `/api/projects/${project.id}${suffix}`; }
function button(text, action, secondary = false) {
  const result = node("button", text, secondary ? "secondary" : "");
  result.type = "button";
  result.addEventListener("click", () => action().catch((error) => notice(error.message)));
  return result;
}
async function listProjects() {
  const values = (await api("/api/projects")).projects;
  $("projects").replaceChildren(...values.map((item) => {
    const result = button(new Date(item.created * 1000).toLocaleString("ja-JP"), async () => {
      if (uploading) throw new Error("素材の送信が終わってから作品を切り替えてください。");
      project = await api(`/api/projects/${item.id}`);
      localStorage.setItem("moment-project", project.id);
      draw();
      await listProjects();
    }, true);
    result.setAttribute("aria-current", String(project?.id === item.id));
    return result;
  }));
}
async function createProject() {
  project = await api("/api/projects", {});
  localStorage.setItem("moment-project", project.id);
  draw();
  await listProjects();
}
function media(asset, local = false) {
  const kind = asset.source?.kind || previews.get(asset.id)?.kind;
  const element = node(kind === "video" ? "video" : "img");
  element.src = local ? previews.get(asset.id).url : path(`/assets/${asset.id}/source`);
  if (kind === "video") { element.controls = true; element.preload = "metadata"; }
  else element.alt = asset.name;
  return element;
}
function drawAssets() {
  $("upload-section").hidden = !project?.assets.length && !waiting.size;
  const rows = (project?.assets || []).map((asset) => {
    const row = node("div", undefined, "asset");
    if (asset.state === "ready" || previews.has(asset.id)) row.append(media(asset, asset.state !== "ready"));
    else row.append(node("span", "素材"));
    const detail = node("div");
    detail.append(node("strong", asset.name));
    const labels = { ready: "確認済み", verifying: "形式を確認しています", failed: asset.error, receiving: "同じ原本を選ぶと送信を再開できます" };
    detail.append(node("p", statuses.get(asset.id) || labels[asset.state]));
    if (asset.state === "receiving") {
      const progress = node("progress");
      progress.max = asset.size; progress.value = asset.offset;
      progress.setAttribute("aria-label", `${asset.name} の送信`);
      detail.append(progress);
    }
    if (!uploading && ["receiving", "failed"].includes(asset.state)) {
      detail.append(button("取り除く", async () => {
        await api(path(`/assets/${asset.id}`), {}, "DELETE");
        if (previews.has(asset.id)) URL.revokeObjectURL(previews.get(asset.id).url);
        previews.delete(asset.id); statuses.delete(asset.id);
        await refresh();
      }, true));
    }
    row.append(detail);
    return row;
  });
  for (const [file, preview] of waiting) {
    const row = node("div", undefined, "asset");
    const element = node(file.type.startsWith("video/") ? "video" : "img");
    element.src = preview;
    if (element.tagName === "VIDEO") { element.controls = true; element.preload = "metadata"; }
    else element.alt = file.name;
    const detail = node("div");
    detail.append(node("strong", file.name), node("p", "送信待ち · このブラウザで表示中"));
    row.append(element, detail); rows.push(row);
  }
  $("assets").replaceChildren(...rows);
}
async function edit(change) {
  const current = project.id;
  const changed = await api(path("/edits"), { revision: project.revision, change });
  if (project.id !== current) return;
  project = changed;
  draw();
}
function drawScenes() {
  $("editor").hidden = !project?.plan;
  if (!project?.plan) { $("scenes").replaceChildren(); return; }
  const frames = project.plan.scenes.reduce((sum, scene) => sum + (scene.included ? (scene.kind === "image" ? scene.duration_frames : scene.end_frame - scene.start_frame) : 0), 0);
  $("duration").textContent = `${(frames / 30).toFixed(1)}秒 · 第${project.revision}版`;
  $("scenes").replaceChildren(...project.plan.scenes.map((scene, index) => {
    const card = node("div", undefined, `scene${scene.included ? "" : " excluded"}`);
    const asset = project.assets.find((item) => item.id === scene.source_id);
    card.append(media(asset));
    const label = node("label");
    const included = node("input"); included.type = "checkbox"; included.checked = scene.included;
    included.addEventListener("change", () => edit({ kind: included.checked ? "include" : "exclude", scene_id: scene.id }).catch((error) => notice(error.message)));
    label.append(included, document.createTextNode(` 場面 ${index + 1} を採用`));
    card.append(label);
    const selectLabel = node("label", "素材を差し替え");
    const select = node("select");
    for (const source of project.assets.filter((item) => item.state === "ready")) {
      const option = node("option", source.name); option.value = source.id; option.selected = source.id === scene.source_id; select.append(option);
    }
    select.addEventListener("change", () => edit({ kind: "replace", scene_id: scene.id, source_id: select.value }).catch((error) => notice(error.message)));
    selectLabel.append(select); card.append(selectLabel);
    if (scene.kind === "video") {
      const trim = node("div");
      const startLabel = node("label", "開始・終了（秒） ");
      const start = node("input"), end = node("input");
      for (const input of [start, end]) { input.type = "number"; input.min = "0"; input.max = String(asset.source.duration_frames / 30); input.step = "0.1"; }
      start.value = (scene.start_frame / 30).toFixed(2); end.value = (scene.end_frame / 30).toFixed(2);
      start.setAttribute("aria-label", `場面${index + 1}の開始秒`); end.setAttribute("aria-label", `場面${index + 1}の終了秒`);
      startLabel.append(start, document.createTextNode(" 〜 "), end);
      trim.append(startLabel, button("前後を調整", () => edit({ kind: "trim", scene_id: scene.id, start_frame: Math.round(Number(start.value) * 30), end_frame: Math.round(Number(end.value) * 30) }), true));
      card.append(trim);
    }
    return card;
  }));
}
function drawJobs() {
  $("output-section").hidden = !project?.jobs.length;
  $("jobs").replaceChildren(...(project?.jobs || []).slice(0, 4).map((job) => {
    const row = node("div", undefined, "job");
    const labels = { queued: "順番を待っています", running: `動画を作成中 ${Math.floor(job.progress * 100)}%`, succeeded: "完成しました", failed: job.error, cancelled: "中止しました" };
    row.append(node("span", `第${job.revision}版 · ${labels[job.state]}`));
    if (["queued", "running"].includes(job.state)) row.append(button("中止", async () => { await api(path(`/jobs/${job.id}/cancel`), {}); await refresh(); }, true));
    if (["failed", "cancelled"].includes(job.state) && job.attempt < 3 && job.revision === project.revision) row.append(button("再試行", async () => { await api(path(`/jobs/${job.id}/retry`), {}); await refresh(); }, true));
    return row;
  }));
  $("output").hidden = !project?.artifact;
  $("download").hidden = !project?.artifact;
  if (project?.artifact) {
    if ($("output").getAttribute("src") !== project.artifact) $("output").src = project.artifact;
    $("download").href = `${project.artifact}?download=1`;
  } else if ($("output").hasAttribute("src")) { $("output").removeAttribute("src"); $("output").load(); }
}
function draw() {
  if (project) $("expiry").textContent = `保存期限 ${new Date(project.expires * 1000).toLocaleString("ja-JP")}。作成から7日間の暫定設定です。編集・ダウンロードによる延長はありません。`;
  drawAssets(); drawScenes(); drawJobs();
}
async function refresh() {
  if (!project) return;
  const current = project.id;
  const latest = await api(path());
  if (project.id !== current) return;
  const revisionChanged = latest.revision !== project.revision;
  project = latest;
  drawAssets(); drawJobs();
  if (revisionChanged) drawScenes();
}
async function hash(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}
async function sendFile(file) {
  let asset = project.assets.find((item) => item.name === file.name && item.size === file.size && item.state === "receiving");
  if (!asset) {
    asset = await api(path("/assets"), { name: file.name, size: file.size });
    project.assets.push(asset);
  }
  if (previews.has(asset.id)) URL.revokeObjectURL(previews.get(asset.id).url);
  previews.set(asset.id, { url: waiting.get(file) || URL.createObjectURL(file), kind: file.type.startsWith("video/") ? "video" : "image" });
  waiting.delete(file);
  drawAssets();
  const endpoint = path(`/assets/${asset.id}`);
  let state = await api(endpoint);
  statuses.set(asset.id, "原本を照合しています"); drawAssets();
  for (const receipt of state.receipts) {
    if (await hash(file.slice(receipt.offset, receipt.offset + receipt.length)) !== receipt.digest) throw new Error(`${file.name} は以前送信した原本と異なります。同じ原本を選び直してください。`);
  }
  let failures = 0;
  while (state.offset < file.size) {
    const chunk = file.slice(state.offset, state.offset + session.limits.chunk_bytes);
    try {
      const result = await api(`${endpoint}/chunks`, chunk, "PUT", { "Upload-Offset": String(state.offset), "X-Chunk-SHA256": await hash(chunk) });
      state.offset = result.offset;
      failures = 0;
    } catch (error) {
      if (++failures > 2) throw error;
      state = await api(endpoint);
      const receipt = state.receipts.at(-1);
      if (receipt && await hash(file.slice(receipt.offset, receipt.offset + receipt.length)) !== receipt.digest) throw new Error("再送内容が一致しません。原本を確認してください。");
    }
    asset.offset = state.offset;
    statuses.set(asset.id, `送信中 ${Math.floor(state.offset / file.size * 100)}%`); drawAssets();
  }
  statuses.set(asset.id, "形式を確認しています"); drawAssets();
  const result = await api(`${endpoint}/finish`, {});
  Object.assign(asset, result);
  statuses.delete(asset.id); drawAssets();
}
async function render() {
  const storageKey = `moment-render-${project.id}-${project.revision}`;
  const key = localStorage.getItem(storageKey) || crypto.randomUUID();
  localStorage.setItem(storageKey, key);
  await api(path("/jobs"), { revision: project.revision, request_key: key });
  await refresh();
}
$("files").addEventListener("change", async (event) => {
  if (uploading) return;
  const files = Array.from(event.target.files);
  if (!files.length) return;
  if (files.length > session.limits.max_assets) { notice("一度に選べる素材は100点までです。"); event.target.value = ""; return; }
  for (const file of files) waiting.set(file, URL.createObjectURL(file));
  uploading = true;
  drawAssets();
  $("files").disabled = true; $("new-project").disabled = true;
  notice();
  const errors = [];
  try {
    if (!project) await createProject();
    for (const file of files) {
      try { await sendFile(file); } catch (error) {
        errors.push(error.message);
        if (waiting.has(file)) { URL.revokeObjectURL(waiting.get(file)); waiting.delete(file); }
      }
    }
    await refresh();
    if (!project.plan && project.assets.some((asset) => asset.state === "ready")) {
      project = await api(path("/draft"), { revision: project.revision });
      draw(); await render();
    }
    notice(errors.join(" "));
  } catch (error) { notice(error.message); }
  finally {
    for (const url of waiting.values()) URL.revokeObjectURL(url);
    waiting.clear();
    uploading = false; $("files").disabled = false; $("new-project").disabled = false; event.target.value = "";
    drawAssets();
  }
});
$("new-project").addEventListener("click", () => createProject().catch((error) => notice(error.message)));
$("render").addEventListener("click", () => render().catch((error) => notice(error.message)));
$("draft").addEventListener("click", async () => {
  try { project = await api(path("/draft"), { revision: project.revision }); draw(); }
  catch (error) { notice(error.message); }
});
async function boot() {
  session = await api("/api/session");
  const saved = localStorage.getItem("moment-project");
  const id = session.projects.find((item) => item.id === saved)?.id || session.projects[0]?.id;
  if (id) { project = await api(`/api/projects/${id}`); draw(); }
  await listProjects();
  setInterval(async () => {
    if (polling || uploading || !project?.jobs.some((job) => ["queued", "running"].includes(job.state))) return;
    polling = true;
    try { await refresh(); } catch (error) { notice(error.message); }
    finally { polling = false; }
  }, 1200);
}
boot().catch((error) => notice(error.message));
