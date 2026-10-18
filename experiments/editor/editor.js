const capability = document.querySelector('meta[name="editor-capability"]').content;
const files = document.querySelector('#files');
const status = document.querySelector('#status');
const error = document.querySelector('#error');
const workspace = document.querySelector('#workspace');
const scenes = document.querySelector('#scenes');
const duration = document.querySelector('#duration');
const preview = document.querySelector('#preview');
const download = document.querySelector('#download');
const refresh = document.querySelector('#refresh');
let snapshot = null;
let selectedFiles = new Map();
let urls = [];
let busy = false;

function setBusy(value) {
  busy = value;
  files.disabled = value;
  refresh.disabled = value || !snapshot?.plan || snapshot.plan.scenes.every(scene => !scene.included);
  for (const control of scenes.querySelectorAll('button, select, input')) control.disabled = value;
}

function message(text, problem = false) {
  (problem ? error : status).textContent = text;
  if (!problem) error.textContent = '';
}

async function request(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Editor-Capability': capability},
    body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作に失敗しました。');
  return value;
}

async function hash(part) {
  const digest = await crypto.subtle.digest('SHA-256', await part.arrayBuffer());
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
}

async function descriptor(file) {
  return {
    name: file.name,
    size: file.size,
    head: await hash(file.slice(0, 65536)),
    tail: await hash(file.slice(Math.max(0, file.size - 65536)))
  };
}

function stale() {
  preview.pause();
  preview.removeAttribute('src');
  preview.load();
  preview.hidden = true;
  document.querySelector('#preview-note').textContent = '編集内容の動画はまだありません。「プレビューを更新」で確認できます。';
  download.removeAttribute('href');
  download.setAttribute('aria-disabled', 'true');
}

function update(value, filesForSources = null) {
  snapshot = value;
  urls.forEach(URL.revokeObjectURL);
  urls = [];
  if (filesForSources) {
    selectedFiles = new Map(value.sources.map((source, index) => [source.id, filesForSources[index]]));
  }
  workspace.hidden = false;
  duration.textContent = `採用 ${value.plan.scenes.filter(scene => scene.included).length}場面 · ${(value.plan.scenes.reduce((sum, scene) => sum + (scene.included ? (scene.kind === 'image' ? scene.duration_frames : scene.end_frame - scene.start_frame) : 0), 0) / 30).toFixed(1)}秒`;
  scenes.replaceChildren();
  for (const [index, scene] of value.plan.scenes.entries()) {
    const source = value.sources.find(item => item.id === scene.source_id);
    const item = document.createElement('li');
    item.className = scene.included ? '' : 'excluded';
    const title = document.createElement('div');
    title.className = 'scene-title';
    const heading = document.createElement('h3');
    heading.textContent = `${index + 1}. ${source.name}`;
    const length = document.createElement('span');
    length.textContent = `${((scene.kind === 'image' ? scene.duration_frames : scene.end_frame - scene.start_frame) / 30).toFixed(1)}秒`;
    title.append(heading, length);
    item.append(title);
    const file = selectedFiles.get(source.id);
    if (file) {
      const media = document.createElement(source.kind === 'image' ? 'img' : 'video');
      const url = URL.createObjectURL(file);
      urls.push(url);
      media.src = url;
      media.className = 'scene-media';
      if (source.kind === 'video') { media.controls = true; media.preload = 'metadata'; media.playsInline = true; }
      else media.alt = source.name;
      item.append(media);
    }
    const controls = document.createElement('div');
    controls.className = 'scene-controls';
    const toggle = document.createElement('button');
    toggle.className = 'secondary';
    toggle.textContent = scene.included ? 'この場面を除外' : 'この場面を採用';
    toggle.addEventListener('click', () => change({kind: scene.included ? 'exclude' : 'include', scene_id: scene.id}));
    controls.append(toggle);
    const label = document.createElement('label');
    label.textContent = '素材を差し替える';
    const select = document.createElement('select');
    for (const candidate of value.sources) {
      const option = document.createElement('option');
      option.value = candidate.id;
      option.textContent = candidate.name;
      option.selected = candidate.id === source.id;
      select.append(option);
    }
    select.addEventListener('change', () => change({kind: 'replace', scene_id: scene.id, source_id: select.value}));
    label.append(select);
    controls.append(label);
    if (scene.kind === 'video') {
      const trim = document.createElement('div');
      trim.className = 'trim';
      const start = document.createElement('input');
      const end = document.createElement('input');
      for (const [input, number, name] of [[start, scene.start_frame, '開始位置（秒）'], [end, scene.end_frame, '終了位置（秒）']]) {
        input.type = 'number'; input.min = '0'; input.max = String(source.duration_frames / 30); input.step = '0.1'; input.value = String(Number((number / 30).toFixed(3)));
        const caption = document.createElement('label');
        caption.textContent = name;
        caption.append(input);
        trim.append(caption);
      }
      const apply = document.createElement('button');
      apply.className = 'secondary';
      apply.textContent = '前後を調整';
      apply.addEventListener('click', () => {
        if (!start.value.trim() || !end.value.trim()) return message('開始・終了位置を秒で入力してください。', true);
        change({kind: 'trim', scene_id: scene.id, start_frame: Math.round(Number(start.value) * 30), end_frame: Math.round(Number(end.value) * 30)});
      });
      trim.append(apply);
      controls.append(trim);
    }
    item.append(controls);
    scenes.append(item);
  }
  if (value.render.status === 'ready' && value.render.revision === value.plan.revision) {
    const url = `/artifact/${value.render.artifact_id}.mp4?cap=${encodeURIComponent(capability)}`;
    preview.src = url;
    preview.hidden = false;
    document.querySelector('#preview-note').textContent = '表示中のプレビューと同じ動画を保存します。';
    download.href = url;
    download.setAttribute('aria-disabled', 'false');
  } else stale();
  setBusy(busy);
}

async function change(payload) {
  if (busy) return;
  setBusy(true);
  try {
    const value = await request('/api/edit', {revision: snapshot.plan.revision, change: payload});
    update(value);
    message('編集を保存しました。プレビューを更新してください。');
  } catch (cause) { message(cause.message, true); }
  finally { setBusy(false); }
}

async function render() {
  if (busy || !snapshot) return;
  setBusy(true);
  message('動画を書き出しています。完了までお待ちください。');
  try {
    update(await request('/api/render', {revision: snapshot.plan.revision}));
    message('プレビューを更新しました。同じMP4を保存できます。');
  } catch (cause) { message(cause.message, true); }
  finally { setBusy(false); }
}

files.addEventListener('change', async () => {
  if (busy || !files.files?.length) return;
  const picked = [...files.files];
  if (picked.length > 100) return message('素材は100点以内で選んでください。', true);
  setBusy(true);
  message('素材を照合しています。');
  try {
    const descriptors = await Promise.all(picked.map(descriptor));
    const value = await request('/api/select', {files: descriptors});
    update(value, picked);
    message('初案を作成しました。動画を書き出しています。');
  } catch (cause) { message(cause.message, true); setBusy(false); return; }
  setBusy(false);
  await render();
});

refresh.addEventListener('click', render);
download.addEventListener('click', event => { if (download.getAttribute('aria-disabled') === 'true') event.preventDefault(); });
