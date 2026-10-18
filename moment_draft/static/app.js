const $ = (id) => document.getElementById(id);
let session;
let project;
let uploading = false;
let polling = false;
let editing = false;
let catalog = [];
let versionConflict = false;
const previews = new Map();
const statuses = new Map();
const waiting = new Map();
const assetRows = new Map();
const trims = new Map();

function notice(message = "") { $("notice").textContent = message; }
function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
function bytes(value) {
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
function seconds(frame) { return (frame / 30).toFixed(1); }
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
  if (!response.ok) {
    const error = new Error(value.error || "処理を完了できませんでした。");
    error.status = response.status;
    throw error;
  }
  return value;
}
function path(suffix = "") { return `/api/projects/${project.id}${suffix}`; }
function button(text, action, secondary = false) {
  const result = node("button", text, secondary ? "secondary" : "");
  result.type = "button";
  result.addEventListener("click", async () => {
    result.disabled = true;
    try { await action(); } catch (error) { notice(error.message); }
    finally { result.disabled = false; }
  });
  return result;
}
function clearLocal() {
  for (const item of previews.values()) URL.revokeObjectURL(item.url);
  for (const item of waiting.values()) URL.revokeObjectURL(item.url);
  previews.clear(); waiting.clear(); statuses.clear(); assetRows.clear(); trims.clear();
  versionConflict = false;
  $("assets").replaceChildren();
}
async function listProjects() {
  const values = (await api("/api/projects")).projects;
  $("projects").replaceChildren(...values.map((item) => {
    const result = button(new Date(item.created * 1000).toLocaleString("ja-JP"), async () => {
      if (uploading || editing) throw new Error("いまの処理が終わってから作品を切り替えてください。");
      if (trims.size) throw new Error("入力中の前後調整を保存するか、最新内容を読み直してから作品を切り替えてください。");
      const next = await api(`/api/projects/${item.id}`);
      clearLocal(); project = next;
      localStorage.setItem("moment-project", project.id);
      notice(); draw();
      await listProjects();
      await initialDraft();
    }, true);
    result.setAttribute("aria-current", String(project?.id === item.id));
    return result;
  }));
}
async function createProject() {
  if (uploading || editing || trims.size) throw new Error("いまの処理や入力中の前後調整を終えてから、新しい作品を作ってください。");
  const next = await api("/api/projects", {});
  clearLocal(); project = next;
  localStorage.setItem("moment-project", project.id);
  notice(); draw();
  await listProjects();
}
function thumbnail(slot, name) {
  const frame = node("div", undefined, "thumbnail");
  frame.dataset.slot = slot;
  const image = node("img"); image.alt = name; image.hidden = true;
  const label = node("span", "画像を準備中");
  image.addEventListener("error", () => { image.hidden = true; label.hidden = false; label.textContent = "画像を表示できません"; });
  frame.append(image, label);
  return frame;
}
function updateThumbnails() {
  for (const frame of document.querySelectorAll(".thumbnail[data-slot]")) {
    const item = project?.thumbnails[frame.dataset.slot];
    const image = frame.querySelector("img"), label = frame.querySelector("span");
    if (item?.url) {
      if (image.getAttribute("src") !== item.url) { image.src = item.url; image.hidden = false; label.hidden = true; }
    } else {
      image.hidden = true; label.hidden = false;
      label.textContent = item?.state === "failed" ? "画像を表示できません" : "画像を準備中";
    }
  }
  $("retry-thumbnails").hidden = !Object.values(project?.thumbnails || {}).some((item) => item.retryable);
}
function assetRow(key, file, preview) {
  if (assetRows.has(key)) return assetRows.get(key);
  const row = node("div", undefined, "asset");
  const visual = node("div", undefined, "asset-visual");
  if (preview) {
    const element = node(file.type?.startsWith("video/") || preview.kind === "video" ? "video" : "img");
    element.src = preview.url;
    if (element.tagName === "VIDEO") { element.controls = true; element.preload = "metadata"; }
    else element.alt = file.name;
    visual.append(element);
  } else visual.append(node("span", "素材"));
  const detail = node("div");
  detail.append(node("strong", file.name), node("p", bytes(file.size), "asset-meta"), node("p", "", "asset-status"));
  const progress = node("progress"); progress.setAttribute("aria-label", `${file.name} の確認済み送信量`);
  detail.append(progress, node("div", undefined, "asset-actions"));
  row.append(visual, detail);
  assetRows.set(key, row); $("assets").append(row);
  return row;
}
function drawAssets() {
  $("upload-section").hidden = !project?.assets.length && !waiting.size;
  const retained = new Set();
  let total = 0, confirmed = 0, ready = 0;
  for (const asset of project?.assets || []) {
    retained.add(asset.id); total += asset.size; confirmed += asset.offset; ready += Number(asset.state === "ready");
    const row = assetRow(asset.id, asset, previews.get(asset.id));
    const labels = { ready: "確認済み", verifying: "形式を確認しています", failed: asset.error, receiving: asset.offset === asset.size ? "送信完了 · 形式の確認を再試行できます" : "同じ原本を選ぶと送信を再開できます" };
    row.querySelector(".asset-status").textContent = statuses.get(asset.id) || labels[asset.state];
    row.querySelector(".asset-meta").textContent = `${bytes(asset.size)}${asset.source ? ` · ${asset.source.width} × ${asset.source.height}${asset.source.kind === "video" ? ` · ${seconds(asset.source.duration_frames)}秒` : " · 写真"}` : " · ブラウザで仮表示"}`;
    const progress = row.querySelector("progress"); progress.max = asset.size; progress.value = asset.offset; progress.hidden = asset.state !== "receiving";
    const visual = row.querySelector(".asset-visual");
    if (asset.state === "ready" && !visual.querySelector(".thumbnail")) visual.replaceChildren(thumbnail(`asset-${asset.id}`, asset.name));
    const actions = row.querySelector(".asset-actions"); actions.replaceChildren();
    if (!uploading && asset.state === "receiving" && asset.offset === asset.size) {
      actions.append(button("確認を再試行", async () => {
        statuses.delete(asset.id);
        try { await api(path(`/assets/${asset.id}/finish`), {}); }
        catch (error) { statuses.set(asset.id, error.message); throw error; }
        finally { await refresh(); }
        await initialDraft();
      }, true));
    }
    if (!uploading && ["receiving", "failed"].includes(asset.state)) {
      actions.append(button("一覧から取り除く", async () => {
        await api(path(`/assets/${asset.id}`), {}, "DELETE");
        if (previews.has(asset.id)) URL.revokeObjectURL(previews.get(asset.id).url);
        previews.delete(asset.id); statuses.delete(asset.id);
        await refresh();
      }, true));
    }
  }
  for (const [file, preview] of waiting) {
    retained.add(file); total += file.size;
    const row = assetRow(file, file, preview);
    row.querySelector(".asset-status").textContent = preview.error || "送信待ち · このブラウザで表示中";
    row.querySelector("progress").hidden = true;
  }
  for (const [key, row] of assetRows) if (!retained.has(key)) { row.remove(); assetRows.delete(key); }
  $("upload-summary").textContent = `${ready} / ${(project?.assets.length || 0) + waiting.size}点確認済み · ${bytes(confirmed)} / ${bytes(total)}送信済み`;
  updateThumbnails();
}
function lockEditor() {
  for (const control of $("editor").querySelectorAll("button,select,input")) control.disabled = editing;
  $("render").disabled = editing || versionConflict || trims.size > 0 || !project?.plan?.scenes.some((scene) => scene.included) || project?.jobs.some((job) => job.revision === project.revision && ["queued", "running"].includes(job.state));
  $("draft").disabled = editing || versionConflict || trims.size > 0;
  $("reload-edit").hidden = !versionConflict && !trims.size;
}
async function edit(change) {
  if (editing) return;
  if (versionConflict) throw new Error("別の画面で編集されました。入力を確認して最新内容を読み直してください。");
  if (trims.size && (change.kind !== "trim" || trims.size !== 1 || !trims.has(change.scene_id))) throw new Error("入力中の前後調整を先に保存してください。");
  editing = true; lockEditor();
  const current = project.id;
  try {
    const changed = await api(path("/edits"), { revision: project.revision, change });
    if (project.id !== current) return;
    project = changed; trims.delete(change.scene_id); notice("編集を保存しました。この内容で動画を作ると、プレビューに反映されます。");
    draw();
  } catch (error) {
    if (error.status === 409) versionConflict = true;
    throw error;
  } finally { editing = false; lockEditor(); }
}
function candidateLabel(candidate) {
  const asset = project.assets.find((item) => item.id === candidate.source_id);
  return `${asset.name} · ${candidate.kind === "image" ? "写真 3秒" : `${seconds(candidate.start_frame)}〜${seconds(candidate.end_frame)}秒`}`;
}
function drawScenes() {
  $("editor").hidden = !project?.plan;
  if (!project?.plan) { $("scenes").replaceChildren(); return; }
  const frames = project.plan.scenes.reduce((sum, scene) => sum + (scene.included ? (scene.kind === "image" ? scene.duration_frames : scene.end_frame - scene.start_frame) : 0), 0);
  $("duration").textContent = `${seconds(frames)}秒 · 第${project.revision}版`;
  $("music").value = project.music.id;
  $("music-description").textContent = catalog.find((track) => track.id === project.music.id)?.description || "元の動画の音だけを残します。写真・無音動画の場面は無音です。";
  $("scenes").replaceChildren(...project.plan.scenes.map((scene, index) => {
    const card = node("div", undefined, `scene${scene.included ? "" : " excluded"}`);
    const asset = project.assets.find((item) => item.id === scene.source_id);
    card.append(thumbnail(`scene-${scene.id}`, `${asset.name} の${scene.kind === "video" ? `${seconds(scene.start_frame)}秒` : "写真"}`));
    const label = node("label");
    const included = node("input"); included.type = "checkbox"; included.checked = scene.included;
    included.addEventListener("change", () => edit({ kind: included.checked ? "include" : "exclude", scene_id: scene.id }).catch((error) => { included.checked = scene.included; notice(error.message); }));
    label.append(included, document.createTextNode(` 場面 ${index + 1} を採用`)); card.append(label);
    card.append(node("p", `${asset.name} · ${scene.kind === "image" ? `${seconds(scene.duration_frames)}秒表示` : `${seconds(scene.start_frame)}〜${seconds(scene.end_frame)}秒を使用`}`, "source-caption"));
    const selectLabel = node("label", "別の素材・時間へ差し替え");
    const select = node("select");
    const prompt = node("option", "候補を選ぶ"); prompt.value = ""; select.append(prompt);
    for (const candidate of project.candidates) {
      const option = node("option", candidateLabel(candidate)); option.value = candidate.id;
      option.selected = candidate.id === scene.candidate_id; select.append(option);
    }
    select.addEventListener("change", () => {
      if (select.value) edit({ kind: "replace", scene_id: scene.id, candidate_id: select.value }).catch((error) => { select.value = scene.candidate_id || ""; notice(error.message); });
    });
    selectLabel.append(select); card.append(selectLabel);
    if (scene.kind === "video") {
      const trim = node("div", undefined, "trim");
      const startLabel = node("label", "開始・終了（秒） ");
      const start = node("input"), end = node("input");
      for (const input of [start, end]) { input.type = "number"; input.min = "0"; input.max = String(asset.source.duration_frames / 30); input.step = "0.1"; }
      const saved = trims.get(scene.id);
      start.value = saved?.start ?? (scene.start_frame / 30).toFixed(2); end.value = saved?.end ?? (scene.end_frame / 30).toFixed(2);
      start.setAttribute("aria-label", `場面${index + 1}の開始秒`); end.setAttribute("aria-label", `場面${index + 1}の終了秒`);
      for (const input of [start, end]) input.addEventListener("input", () => {
        trims.set(scene.id, { start: start.value, end: end.value });
        notice("前後調整を入力中です。「前後を保存」で保存してください。"); lockEditor();
      });
      startLabel.append(start, document.createTextNode(" 〜 "), end);
      trim.append(startLabel, button("前後を保存", () => edit({ kind: "trim", scene_id: scene.id, start_frame: Math.round(Number(start.value) * 30), end_frame: Math.round(Number(end.value) * 30) }), true));
      card.append(trim);
    }
    return card;
  }));
  updateThumbnails(); lockEditor();
}
function drawJobs() {
  $("output-section").hidden = !project?.plan && !project?.jobs.length;
  $("output-version").textContent = project?.plan ? `第${project.revision}版` : "";
  const active = project?.jobs.find((job) => job.revision === project.revision && ["queued", "running"].includes(job.state));
  $("output-status").textContent = versionConflict ? "別の画面の編集を確認してください。" : project?.artifact ? "いまの編集内容で完成しました。" : active ? "完成したら、ここにプレビューが表示されます。" : "編集内容は保存済みです。「この内容で動画を作る」でプレビューを更新してください。";
  $("jobs").replaceChildren(...(project?.jobs || []).slice(0, 4).map((job) => {
    const row = node("div", undefined, "job");
    const labels = { queued: "順番を待っています", running: job.progress >= .95 ? "映像と音声を確認しています" : `動画を作成中 ${Math.floor(job.progress * 100)}%`, succeeded: job.revision === project.revision ? "完成しました" : "以前の編集内容です", failed: job.error, cancelled: "中止しました" };
    row.append(node("span", `第${job.revision}版 · ${labels[job.state]}`));
    if (["queued", "running"].includes(job.state)) row.append(button("中止", async () => { await api(path(`/jobs/${job.id}/cancel`), {}); await refresh(); }, true));
    if (["failed", "cancelled"].includes(job.state) && job.attempt < 3 && job.revision === project.revision) row.append(button("再試行", async () => { await api(path(`/jobs/${job.id}/retry`), {}); await refresh(); }, true));
    return row;
  }));
  const artifact = !versionConflict && project?.artifact;
  $("output").hidden = !artifact; $("download").hidden = !artifact;
  if (artifact) {
    if ($("output").getAttribute("src") !== artifact) $("output").src = artifact;
    $("download").href = `${artifact}?download=1`;
  } else {
    $("download").removeAttribute("href");
    if ($("output").hasAttribute("src")) { $("output").removeAttribute("src"); $("output").load(); }
  }
  lockEditor();
}
function draw() {
  if (project) $("expiry").textContent = `保存期限 ${new Date(project.expires * 1000).toLocaleString("ja-JP")}。作成から7日間の暫定設定です。編集・ダウンロードによる延長はありません。`;
  drawAssets(); drawScenes(); drawJobs();
}
async function refresh() {
  if (!project) return;
  const current = project.id, revision = project.revision;
  const latest = await api(path());
  if (project.id !== current || project.revision !== revision || editing) return;
  const revisionChanged = latest.revision !== project.revision;
  if (revisionChanged && trims.size) {
    versionConflict = true; project.artifact = null;
    notice("別の画面で編集されました。入力中の前後調整はこの画面に残しています。最新内容を読み直すと入力は破棄されます。");
    drawJobs(); return;
  }
  const added = latest.assets.filter((asset) => asset.state === "ready").length > project.assets.filter((asset) => asset.state === "ready").length;
  project = latest;
  if (added && project.plan) $("new-assets").hidden = false;
  drawAssets(); drawJobs();
  if (revisionChanged || added) drawScenes();
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
  const preview = waiting.get(file);
  if (previews.has(asset.id)) URL.revokeObjectURL(previews.get(asset.id).url);
  previews.set(asset.id, { url: preview.url, kind: file.type.startsWith("video/") ? "video" : "image" });
  if (assetRows.has(file)) {
    assetRows.get(asset.id)?.remove();
    assetRows.set(asset.id, assetRows.get(file)); assetRows.delete(file);
  }
  waiting.delete(file);
  const endpoint = path(`/assets/${asset.id}`);
  try {
    let state = await api(endpoint);
    statuses.set(asset.id, "原本を照合しています"); drawAssets();
    for (const receipt of state.receipts) {
      if (await hash(file.slice(receipt.offset, receipt.offset + receipt.length)) !== receipt.digest) throw new Error("以前送信した原本と異なります。同じ原本を選び直してください。");
    }
    let failures = 0;
    while (state.offset < file.size) {
      const chunk = file.slice(state.offset, state.offset + session.limits.chunk_bytes);
      try {
        const result = await api(`${endpoint}/chunks`, chunk, "PUT", { "Upload-Offset": String(state.offset), "X-Chunk-SHA256": await hash(chunk) });
        state.offset = result.offset; failures = 0;
      } catch (error) {
        if (++failures > 2) throw error;
        state = await api(endpoint);
        const receipt = state.receipts.at(-1);
        if (receipt && await hash(file.slice(receipt.offset, receipt.offset + receipt.length)) !== receipt.digest) throw new Error("再送内容が一致しません。原本を確認してください。");
      }
      asset.offset = state.offset;
      statuses.set(asset.id, `送信中 ${Math.floor(state.offset / file.size * 100)}% · 確認済み ${bytes(state.offset)}`); drawAssets();
    }
    statuses.set(asset.id, "形式を確認しています"); drawAssets();
    Object.assign(asset, await api(`${endpoint}/finish`, {}));
    statuses.delete(asset.id);
    if (asset.state === "failed") throw new Error(asset.error);
  } catch (error) {
    statuses.set(asset.id, error.message);
    throw new Error(`${file.name} · ${error.message}`);
  } finally { drawAssets(); }
}
async function render() {
  if (trims.size || editing || versionConflict) throw new Error("入力中の変更を保存してから動画を作ってください。");
  const previous = project.jobs.find((job) => job.revision === project.revision);
  if (previous && ["failed", "cancelled"].includes(previous.state)) {
    await api(path(`/jobs/${previous.id}/retry`), {});
    await refresh(); return;
  }
  const storageKey = `moment-render-${project.id}-${project.revision}`;
  const key = localStorage.getItem(storageKey) || crypto.randomUUID();
  localStorage.setItem(storageKey, key);
  await api(path("/jobs"), { revision: project.revision, request_key: key });
  await refresh();
}
async function initialDraft() {
  if (!project?.assets.some((asset) => asset.state === "ready")) return;
  if (!project.plan) {
    try { project = await api(path("/draft"), { revision: project.revision }); }
    catch (error) { if (error.status !== 409) throw error; await refresh(); }
    draw();
  }
  if (project.revision === 1 && !project.jobs.length) await render();
}
$("files").addEventListener("change", async (event) => {
  if (uploading) return;
  const files = Array.from(event.target.files);
  if (!files.length) return;
  if (files.length > session.limits.max_assets) { notice("一度に選べる素材は100点までです。"); event.target.value = ""; return; }
  try { if (!project) await createProject(); } catch (error) { notice(error.message); return; }
  for (const file of files) {
    for (const [old, preview] of waiting) if (old.name === file.name && old.size === file.size) {
      URL.revokeObjectURL(preview.url); waiting.delete(old);
    }
    waiting.set(file, { url: URL.createObjectURL(file) });
  }
  uploading = true; drawAssets();
  $("files").disabled = true; $("new-project").disabled = true;
  $("upload-details").open = true;
  notice();
  const errors = [], hadPlan = Boolean(project.plan);
  try {
    for (const file of files) {
      try { await sendFile(file); }
      catch (error) { errors.push(error.message); if (waiting.has(file)) waiting.get(file).error = `${error.message} 原本を選び直すと再試行できます。`; }
    }
    await refresh(); drawScenes();
    if (hadPlan) $("new-assets").hidden = false;
    await initialDraft();
    notice(errors.join(" "));
  } catch (error) { notice(error.message); }
  finally {
    uploading = false; $("files").disabled = false; $("new-project").disabled = false; event.target.value = "";
    drawAssets();
  }
});
$("new-project").addEventListener("click", () => createProject().catch((error) => notice(error.message)));
$("render").addEventListener("click", () => render().catch((error) => notice(error.message)));
$("draft").addEventListener("click", async () => {
  if (editing || trims.size || versionConflict) return;
  editing = true; lockEditor();
  try { project = await api(path("/draft"), { revision: project.revision }); $("new-assets").hidden = true; notice("全素材から初案を作り直しました。内容を確認して動画を作ってください。"); draw(); }
  catch (error) { notice(error.message); }
  finally { editing = false; lockEditor(); }
});
$("music").addEventListener("change", () => edit({ kind: "music", id: $("music").value }).catch((error) => { $("music").value = project.music.id; notice(error.message); }));
$("reload-edit").addEventListener("click", async () => {
  try { const latest = await api(path()); trims.clear(); versionConflict = false; project = latest; notice("最新の編集内容を読み直しました。"); draw(); }
  catch (error) { notice(error.message); }
});
$("retry-thumbnails").addEventListener("click", async () => {
  try { await api(path("/thumbnails/retry"), {}); await refresh(); }
  catch (error) { notice(error.message); }
});
async function boot() {
  session = await api("/api/session");
  catalog = (await api("/api/music")).tracks;
  $("music").append(...[{ id: "off", name: "BGMなし" }, ...catalog].map((track) => { const option = node("option", track.name); option.value = track.id; return option; }));
  const saved = localStorage.getItem("moment-project");
  const id = session.projects.find((item) => item.id === saved)?.id || session.projects[0]?.id;
  if (id) { project = await api(`/api/projects/${id}`); draw(); }
  await listProjects();
  await initialDraft();
  setInterval(async () => {
    if (polling || uploading || editing || !project) return;
    polling = true;
    try { await refresh(); } catch (error) { notice(error.message); }
    finally { polling = false; }
  }, 1800);
}
boot().catch((error) => notice(error.message));
