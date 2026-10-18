# Local service API

This service accepts browser uploads into one private data directory. SQLite owns project access, upload offsets, immutable edits and render jobs. The browser keeps only the current project ID and render request keys. It must reselect original files after a reload to resume an incomplete upload.

The first service slice supports local use. Public deployment, account recovery, invite admission, external AI scoring and payments are not implemented. The BGM catalog is empty in this slice; the renderer accepts a server-owned music path for later integration.

## Start and stop

Use Python 3.11, install `requirements.txt`, and provide a native FFmpeg executable with libx264 and AAC support.

```sh
MOMENT_FFMPEG=/absolute/path/to/ffmpeg python -m moment_draft
```

Open `http://127.0.0.1:8770`. SIGTERM and Ctrl-C stop the web server, cancel active native work, join the worker and release the data-directory lock. A second service or cleanup process cannot open the same volume while the service runs.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MOMENT_DATA_DIR` | `.local/service-data` | Private persistent directory |
| `MOMENT_FFMPEG` | `ffmpeg` | Operator-provided executable |
| `MOMENT_ORIGIN` | `http://127.0.0.1:8770` | Exact accepted Host and Origin |
| `MOMENT_HOST` | `127.0.0.1` | Listen address |
| `MOMENT_PORT` | `8770` | Must match the origin port |
| `MOMENT_RETENTION_DAYS` | `7` | Expiry from project creation, with no extension |
| `MOMENT_CONTAINER_LOCAL` | unset | `1` permits an internal `0.0.0.0` bind with a loopback HTTP origin |

Nonloopback origins fail at startup. The container exception does not add public authentication. Its operator must publish the container port only on host loopback. `MOMENT_ACCESS_CODE` is reserved and does not enable public mode.

## Sessions and boundaries

`GET /api/session` creates a random 256-bit owner secret in an HttpOnly, SameSite=Strict cookie. SQLite stores only its SHA-256 hash. The response supplies a session-bound CSRF nonce, owned projects and limits. An existing valid cookie restores its owner after restart. Cookies are persistent for one year; projects retain their own shorter expiry.

Every API request except session bootstrap needs the owner cookie. Every write needs the exact configured `Origin` and `X-CSRF-Token`. Every request needs the exact configured `Host`; an explicit foreign Origin or cross-site Fetch Metadata value is rejected. There is no CORS support or capability in a URL. Private responses use `Cache-Control: no-store, private`. `/healthz` returns only `{"status":"ok"}` and still checks Host.

Names are display data. The server creates random project, asset and job IDs and derives storage paths from its database. Request bodies cannot provide paths, source URLs, filters, codecs or encoder arguments. The app is served with a same-origin content security policy.

## Routes

JSON write bodies are limited to 64 KiB when Content-Length is present. Waitress and Flask both cap every request at 8 MiB. Binary chunks use `application/octet-stream`. All errors return an `error` message, except an early request-size rejection from Waitress and an empty 416 range response.

| Method | Path | Body or result |
| --- | --- | --- |
| GET | `/api/session` | CSRF nonce, projects, limits; sets cookie if needed |
| GET | `/api/projects` | Owned unexpired project summaries |
| POST | `/api/projects` | `{}` creates a project and concrete expiry |
| GET | `/api/projects/<p>` | Assets, current plan/music, jobs and current artifact URL |
| POST | `/api/projects/<p>/assets` | `{name,size}` reserves an asset and capacity |
| GET | `/api/projects/<p>/assets/<a>` | State, offset and ordered chunk receipts |
| PUT | `/api/projects/<p>/assets/<a>/chunks` | Binary data; `Upload-Offset` and optional `X-Chunk-SHA256` headers |
| POST | `/api/projects/<p>/assets/<a>/finish` | `{}` verifies the complete local file |
| GET, HEAD | `/api/projects/<p>/assets/<a>/source` | Verified owned original with Range support |
| POST | `/api/projects/<p>/draft` | `{revision}` creates an immutable metadata baseline |
| POST | `/api/projects/<p>/edits` | `{revision,change}` applies one scene edit |
| POST | `/api/projects/<p>/jobs` | `{revision,request_key}` queues a render, returns 202 |
| GET | `/api/projects/<p>/jobs/<j>` | Job state, attempt, progress, safe error |
| POST | `/api/projects/<p>/jobs/<j>/cancel` | `{}` cancels queued work or requests running cancellation |
| POST | `/api/projects/<p>/jobs/<j>/retry` | `{}` explicitly retries failed/cancelled work |
| GET, HEAD | `/api/projects/<p>/jobs/<j>/artifact` | Current successful MP4 with Range support |
| GET | `/api/music` | `{tracks:[]}` in the initial slice |

Append `?download=1` to the artifact route for a download disposition. Preview and download read the same checked MP4. Single byte ranges support closed, open-ended and suffix forms. Invalid, multiple and unsatisfiable ranges return 416. Foreign owners receive 404 for projects, jobs and files, including Range requests. Expired projects receive 404 before physical cleanup. Old revision outputs cannot be downloaded once a new edit is current.

Edit changes use `kind` and `scene_id`. `include` and `exclude` toggle adoption. `replace` takes a ready asset's `source_id`. `trim` takes integer `start_frame` and `end_frame`, at 30 fps and within that video's verified duration. Changes use compare-and-swap against the current revision. Stale edits return 409. Rebuilding a draft creates another revision. A render job retains its immutable revision and source snapshot even when editing continues.

## Upload and render recovery

Assets move through `receiving`, `verifying`, `ready` or `failed`. Registration reserves declared bytes transactionally. A chunk can append only at the confirmed offset. The service computes SHA-256, writes and fsyncs bytes, then commits the offset and immutable receipt. Replaying a matching receipt returns the current offset. Changed bytes at a received offset return 409. Restart truncates any bytes beyond the confirmed offset. A process interrupted during verification returns the asset to `receiving`, allowing an idempotent finish retry.

The browser hashes each acknowledged chunk of a reselected original before appending. It never loads an entire multi-gigabyte original into one ArrayBuffer. Uploads use at most 8 MiB per request. A lost chunk acknowledgement can be resolved by GETting the asset and comparing its receipt. An incomplete finish returns 409. Invalid media leaves a per-file failure while other ready assets remain usable.

The native probe checks file signatures, decodes a frame, and checks dimensions, duration and HDR transfer metadata. It runs in a subprocess with a deadline. Accepted formats in this slice are JPEG, PNG, WebP and SDR ISO-BMFF videos such as MP4/MOV. HDR is refused; a 10-bit format alone is not treated as HDR. HEIC support is not claimed.

Jobs move through `queued`, `running`, `succeeded`, `failed` or `cancelled`. A repeated project/request-key pair returns the same job; the same key with a different revision returns 409. Retries preserve the input snapshot and increment the attempt count. A random token fences each attempt. Native work runs outside SQLite transactions, uses local-file protocols, and has a deadline. A supervisor terminates FFmpeg when its parent pipe closes, including abrupt service death. Cancellation terminates the native process group. Restart marks interrupted running jobs as failed and removes abandoned service-owned attempt directories.

Each attempt uses its own temporary directory. Photos are scaled once before looping. The renderer produces 1280×720, 30 fps H.264/AAC with contain padding, automatic orientation, original video sound and silence for stills/silent videos. It decodes output frames and checks frame count and audio duration before publishing. Source identity, attempt token, cancellation and expiry are checked again before atomic rename and successful status. Rename followed by an interrupted database commit cannot expose an uncommitted output. Its job remains failed after restart and requires explicit retry.

## Trial limits and retention

Defaults are configured limits, not measured capacity. There are at most 100 assets, 20 GiB declared source bytes and 50 render jobs per project; an asset is at most 8 GiB. There are at most 10 active projects per owner, 100 retained projects per host and 100 GiB declared source bytes per host. The global render queue allows 10 queued/running jobs and one worker. There are two active upload/finish requests per owner, three render attempts per job, a 120-second output limit, a 30-second probe deadline and a 15-minute render deadline. New reservations account for other unfilled reservations and preserve a 10 GiB free-space floor. Admission rejects work without discarding existing edits.

Expiry is provisionally creation plus seven days. It does not extend on edits or downloads. Access checks enforce expiry immediately. Stop the service before using the standalone cleanup command.

```sh
python -m moment_draft cleanup
python -m moment_draft cleanup --apply
```

The first command lists expired project IDs without deleting them. `--apply` cancels queued work, requests cancellation of active work and deletes expired service-owned project directories only after active render/verification work releases them. It never operates on the original browser files. Scheduling cleanup and monitoring storage are operator responsibilities.

## Verification

```sh
MOMENT_FFMPEG=/absolute/path/to/ffmpeg EDITOR_FFMPEG=/absolute/path/to/ffmpeg python -m unittest discover -s tests -v
```

Service tests upload generated small media, replay chunks, recover an uncommitted tail, edit, render and decode output. They check portrait padding, original sound, preview/download identity, private Range access, CAS, expiry, quotas, cancellation and attempt fencing. Process tests start actual Waitress processes, kill and restart them, retain the same owner cookie, resume an upload and retrieve the same completed output after another restart. A separate native test kills a parent process and verifies its FFmpeg child exits. The original eight experiment tests remain unchanged.

Browser usability, event selection quality, private real-media performance, public deployment security, codec breadth and container operation require separate evidence. API and native tests do not prove those gates.
