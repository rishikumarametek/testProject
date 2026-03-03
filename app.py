import os
import socket
import struct
import threading
import time
import hashlib
import tempfile
import uuid
import shutil
from typing import Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# -------------------------
# Constants (match C# / Tk)
# -------------------------
MAGIC = b"FTR1"                 # 4 bytes
CHUNK_SIZE = 1 << 16            # 64 KB
PROGRESS_MAX = 1000             # permille (0..1000)
DEFAULT_HOST = "192.168.1.50"
DEFAULT_PORT = 5000

# -------------------------
# App & Task State
# -------------------------
app = FastAPI(title="TCP File Sender (FastAPI)")
# Optional: allow cross-origin if you host UI separately
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# task registry in-memory (single-process)
_tasks_lock = threading.Lock()
_tasks: Dict[str, Dict[str, Any]] = {}

def _new_task() -> str:
    return uuid.uuid4().hex

def _set_task(task_id: str, **kwargs):
    with _tasks_lock:
        if task_id not in _tasks:
            _tasks[task_id] = {}
        _tasks[task_id].update(kwargs)

def _get_task(task_id: str) -> Dict[str, Any]:
    with _tasks_lock:
        if task_id not in _tasks:
            raise KeyError("Task not found")
        # return a shallow copy to avoid mutation from callers
        return dict(_tasks[task_id])

def _checked_sendall(sock: socket.socket, data: bytes, cancel_event: threading.Event):
    """sendall that checks for cancel between chunks."""
    view = memoryview(data)
    total = len(data)
    sent = 0
    while sent < total:
        if cancel_event.is_set():
            raise RuntimeError("Transfer canceled by user.")
        n = sock.send(view[sent:])
        if n <= 0:
            raise IOError("Socket send failed.")
        sent += n

def _send_file_task(
    task_id: str,
    host: str,
    port: int,
    tmp_path: str,
    original_filename: str,
    cancel_event: threading.Event,
):
    sock = None
    try:
        file_name = os.path.basename(original_filename)
        file_name_bytes = file_name.encode("utf-8")
        file_size = os.path.getsize(tmp_path)

        # init task metrics
        _set_task(
            task_id,
            status="starting",
            bytes_sent=0,
            bytes_total=file_size,
            progress_permille=0,
            label=f"0.0% (0 / {file_size/1024/1024:.2f} MiB) 0.00 MiB/s",
            sha256=None,
            error=None,
            started_at=time.time(),
            ended_at=None,
        )

        # Connect
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, CHUNK_SIZE)
        sock.settimeout(15)
        _set_task(task_id, status="connecting", message=f"Connecting to {host}:{port}…")
        sock.connect((host, port))

        # Header
        sha256 = hashlib.sha256()
        header = bytearray()
        header += MAGIC
        header += struct.pack(">H", len(file_name_bytes))  # uint16 BE
        header += file_name_bytes
        header += struct.pack(">Q", file_size)             # uint64 BE
        _checked_sendall(sock, header, cancel_event)

        # Body
        sent = 0
        start = time.monotonic()
        last_report = 0.0

        _set_task(
            task_id,
            status="sending",
            progress_permille=0,
            label=f"0.0% (0 / {file_size/1024/1024:.2f} MiB) 0.00 MiB/s",
        )

        with open(tmp_path, "rb") as f:
            while sent < file_size:
                if cancel_event.is_set():
                    raise RuntimeError("Transfer canceled by user.")

                to_read = min(CHUNK_SIZE, file_size - sent)
                chunk = f.read(to_read)
                if not chunk:
                    raise IOError("Unexpected end of file while reading.")
                sha256.update(chunk)
                _checked_sendall(sock, chunk, cancel_event)
                sent += len(chunk)

                now = time.monotonic()
                if (now - last_report) >= 0.25 or sent == file_size:
                    pct = (100.0 if file_size == 0 else (sent * 100.0 / file_size))
                    mb_sent = sent / 1024 / 1024
                    mb_total = file_size / 1024 / 1024
                    elapsed = max(1e-9, now - start)
                    mbps = (sent / 1024 / 1024) / elapsed
                    permille = (PROGRESS_MAX if file_size == 0
                                else round(sent * PROGRESS_MAX / file_size))
                    _set_task(
                        task_id,
                        bytes_sent=sent,
                        progress_permille=permille,
                        label=f"{pct:.1f}% ({mb_sent:.2f}/{mb_total:.2f} MiB) Avg: {mbps:.2f} MiB/s",
                    )
                    last_report = now

        # Trailer: SHA-256
        digest = sha256.digest()
        _checked_sendall(sock, digest, cancel_event)
        try:
            sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass

        _set_task(
            task_id,
            status="completed",
            sha256=digest.hex(),
            progress_permille=PROGRESS_MAX,
            label="100.0% (Completed + Digest Sent)",
            ended_at=time.time(),
        )

    except Exception as ex:
        status = "canceled" if isinstance(ex, RuntimeError) and "canceled" in str(ex).lower() else "error"
        _set_task(
            task_id,
            status=status,
            error=str(ex),
            progress_permille=0 if status != "completed" else PROGRESS_MAX,
            ended_at=time.time(),
        )
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
        # Cleanup temp file
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


# -------------------------
# Routes
# -------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    # Minimal, self-contained HTML UI with fetch + polling for progress
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TCP File Sender (Web)</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }}
label {{ display:block; margin-top: 0.75rem; }}
input[type="text"], input[type="number"] {{ padding: 0.4rem; width: 18rem; }}
button {{ padding: 0.5rem 1rem; margin-top: 1rem; }}
.progress-wrap {{ width: 100%; max-width: 600px; background: #eee; border-radius: 6px; height: 16px; margin-top: 1rem; }}
.progress-bar {{ height: 100%; background: #3b82f6; width: 0%; border-radius: 6px; transition: width 0.2s; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.small {{ color: #555; font-size: 0.9rem; }}
.status {{ margin-top: 0.5rem; }}
</style>
</head>
<body>
  <h1>TCP File Sender (FastAPI)</h1>
  <form id="sendForm">
    <label>Host/IP:
      <input type="text" name="host" value="{DEFAULT_HOST}" required />
    </label>
    <label>Port:
      <input type="number" name="port" value="{DEFAULT_PORT}" min="1" max="65535" required />
    </label>
    <label>File:
      <input type="file" name="file" required />
    </label>
    <div>
      <button id="startBtn" type="submit">Send</button>
      <button id="cancelBtn" type="button" disabled>Cancel</button>
    </div>
  </form>

  <div class="progress-wrap" aria-label="Progress">
    <div class="progress-bar" id="bar"></div>
  </div>
  <div class="status mono" id="status">Idle</div>
  <div class="small mono" id="details"></div>

<script>
const form = document.getElementById('sendForm');
const bar = document.getElementById('bar');
const statusEl = document.getElementById('status');
const detailsEl = document.getElementById('details');
const startBtn = document.getElementById('startBtn');
const cancelBtn = document.getElementById('cancelBtn');

let taskId = null;
let timer = null;

function setProgress(permille, label) {{
  const pct = Math.max(0, Math.min(1000, permille)) / 10.0;
  bar.style.width = pct + '%';
  statusEl.textContent = label || '';
}}

function poll() {{
  if (!taskId) return;
  fetch('/api/progress/' + taskId)
    .then(r => r.json())
    .then(j => {{
      const t = j;
      setProgress(t.progress_permille || 0, t.label || t.status || '...');
      detailsEl.textContent = 'Status: ' + t.status + (t.sha256 ? (' | SHA-256: ' + t.sha256) : '') + (t.error ? (' | Error: ' + t.error) : '');
      if (t.status === 'completed' || t.status === 'error' || t.status === 'canceled') {{
        clearInterval(timer); timer = null;
        cancelBtn.disabled = true;
        startBtn.disabled = false;
      }}
    }})
    .catch(_ => {{}});
}}

cancelBtn.addEventListener('click', () => {{
  if (!taskId) return;
  fetch('/api/cancel/' + taskId, {{ method: 'POST' }})
    .catch(_ => {{}})
    .finally(() => {{
      cancelBtn.disabled = true;
    }});
}});

form.addEventListener('submit', (e) => {{
  e.preventDefault();
  const data = new FormData(form);
  startBtn.disabled = true;
  cancelBtn.disabled = true;
  statusEl.textContent = 'Starting...';
  detailsEl.textContent = '';
  setProgress(0, 'Starting...');
  fetch('/api/send', {{ method: 'POST', body: data }})
    .then(r => {{
      if (!r.ok) throw new Error('Failed to start');
      return r.json();
    }})
    .then(j => {{
      taskId = j.task_id;
      cancelBtn.disabled = false;
      statusEl.textContent = 'Connecting...';
      if (timer) clearInterval(timer);
      timer = setInterval(poll, 500);
      poll();
    }})
    .catch(err => {{
      startBtn.disabled = false;
      statusEl.textContent = 'Error: ' + err.message;
    }});
}});
</script>
</body>
</html>
    """)

@app.post("/api/send")
async def api_send(
    host: str = Form(...),
    port: int = Form(...),
    file: UploadFile = File(...),
):
    host = host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="Please provide a host/IP.")
    if not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail="Please provide a valid port (1–65535).")

    # We need file size for header; UploadFile may not have a reliable size.
    # Safest: spooled to a temp file first.
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp_path = tmp.name
        with tmp:
            # Copy to temp file in chunks to avoid memory blowup
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                tmp.write(chunk)
    finally:
        await file.close()

    # Prepare task and thread
    task_id = _new_task()
    cancel_event = threading.Event()
    _set_task(task_id, status="queued", message="Queued", cancel_event=cancel_event)

    t = threading.Thread(
        target=_send_file_task,
        args=(task_id, host, port, tmp_path, file.filename or os.path.basename(tmp_path), cancel_event),
        daemon=True
    )
    t.start()

    return JSONResponse({"task_id": task_id})

# For Testig Purpose Only
@app.get("/api/tasks")
def api_tasks():
    with _tasks_lock:
        return JSONResponse({tid: dict(data) for tid, data in _tasks.items()})
    
@app.get("/api/progress/{task_id}")
def api_progress(task_id: str):
    try:
        data = _get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")

    # Do not return internal objects (like Event)
    data.pop("cancel_event", None)
    return JSONResponse(data)

@app.post("/api/cancel/{task_id}")
def api_cancel(task_id: str):
    try:
        data = _get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    ev = data.get("cancel_event")
    if isinstance(ev, threading.Event):
        ev.set()
        _set_task(task_id, status="canceling", message="Cancel requested…")
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False})