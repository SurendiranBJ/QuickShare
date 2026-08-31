import os
import sys
import time
import uuid
import json
import shutil
import hashlib
import re
import math
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from flask import Flask, request, send_from_directory, render_template_string, jsonify, abort
from werkzeug.utils import secure_filename

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("QuickShare")

# -----------------------------------------------------------------------------
# Configuration Constants
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
DEFAULT_CHUNK_SIZE = int(os.getenv("DEFAULT_CHUNK_SIZE", 5 * 1024 * 1024))  # 5 MB
UPLOAD_CACHE_TIMEOUT = int(os.getenv("UPLOAD_CACHE_TIMEOUT", 21600))        # 6 hours (configurable via env)
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 1800))                 # Check expired cache every 30 mins
UUID_REGEX = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.IGNORECASE)

# Ensure directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)
# Allow large chunk payloads (up to 100MB per chunk if configured)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# -----------------------------------------------------------------------------
# Per-Upload Fine-Grained Locking Manager
# -----------------------------------------------------------------------------
class UploadLockManager:
    """Provides thread-safe per-upload locks without global contention or memory leaks."""
    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}  # upload_id -> [Lock, ref_count]
        self._filename_collision_lock = threading.Lock()

    @contextmanager
    def acquire(self, upload_id):
        if not upload_id:
            yield
            return

        with self._guard:
            if upload_id not in self._locks:
                self._locks[upload_id] = [threading.Lock(), 0]
            entry = self._locks[upload_id]
            entry[1] += 1
            lock = entry[0]

        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                entry[1] -= 1
                if entry[1] <= 0 and upload_id in self._locks:
                    del self._locks[upload_id]

    @contextmanager
    def filename_lock(self):
        """Global lock used only during final non-colliding filename determination."""
        with self._filename_collision_lock:
            yield

upload_lock_manager = UploadLockManager()

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
def format_bytes(size_bytes):
    """Format bytes into a human readable string (KB, MB, GB, etc.)."""
    if size_bytes is None or size_bytes < 0:
        return "0 B"
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    i = min(i, len(units) - 1)
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {units[i]}"

def is_valid_uuid(val):
    """Check if the value is a valid UUID string."""
    return bool(val and isinstance(val, str) and UUID_REGEX.match(val))

def is_safe_path(base_directory, target_path):
    """Verify that target_path is strictly contained within base_directory."""
    try:
        base_abs = os.path.abspath(base_directory)
        target_abs = os.path.abspath(target_path)
        return os.path.commonpath([base_abs, target_abs]) == base_abs
    except (ValueError, Exception):
        return False

def sanitize_filename(filename):
    """Sanitize filename securely while preserving original extension."""
    if not filename:
        return f"file_{uuid.uuid4().hex[:8]}"
    
    clean_name = os.path.basename(filename).strip()
    safe_name = secure_filename(clean_name)
    
    if not safe_name:
        _, ext = os.path.splitext(clean_name)
        safe_ext = secure_filename(ext)
        safe_name = f"file_{uuid.uuid4().hex[:8]}{safe_ext}"
    return safe_name

def get_unique_filename(destination_dir, filename):
    """Generate a non-colliding filename if a file with the same name already exists."""
    safe_name = sanitize_filename(filename)
    target_path = os.path.join(destination_dir, safe_name)
    
    if not os.path.exists(target_path):
        return safe_name
        
    base, ext = os.path.splitext(safe_name)
    counter = 1
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(os.path.join(destination_dir, candidate)):
            return candidate
        counter += 1

def get_upload_cache_dir(upload_id):
    """Get absolute path to an upload's cache directory with strict path containment check."""
    if not is_valid_uuid(upload_id):
        return None
    cache_path = os.path.join(CACHE_DIR, upload_id)
    if not is_safe_path(CACHE_DIR, cache_path):
        return None
    return cache_path

def get_metadata_path(upload_id):
    """Get path to metadata.json for an upload."""
    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir:
        return None
    return os.path.join(cache_dir, "metadata.json")

def get_chunks_dir(upload_id):
    """Get path to chunks directory for an upload."""
    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir:
        return None
    return os.path.join(cache_dir, "chunks")

def load_metadata(upload_id):
    """Safely load metadata.json for an upload session."""
    meta_path = get_metadata_path(upload_id)
    if not meta_path or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data["received_chunks"] = set(data.get("received_chunks", []))
            return data
    except Exception as e:
        logger.error(f"Error reading metadata for upload_id={upload_id}: {e}")
        return None

def save_metadata(upload_id, metadata):
    """Atomically save metadata.json for an upload session."""
    meta_path = get_metadata_path(upload_id)
    if not meta_path:
        return False
    tmp_path = f"{meta_path}.tmp_{uuid.uuid4().hex[:6]}"
    try:
        serializable = metadata.copy()
        if isinstance(serializable.get("received_chunks"), (set, list)):
            serializable["received_chunks"] = sorted(list(serializable["received_chunks"]))
        
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            
        os.replace(tmp_path, meta_path)
        return True
    except Exception as e:
        logger.error(f"Error saving metadata for upload_id={upload_id}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False

def clean_expired_cache():
    """Scans cache directory and cleans up abandoned uploads safely with lock."""
    if not os.path.exists(CACHE_DIR):
        return
    now = time.time()
    count_cleaned = 0
    try:
        for entry in os.listdir(CACHE_DIR):
            if not is_valid_uuid(entry):
                continue
            item_path = os.path.join(CACHE_DIR, entry)
            if not os.path.isdir(item_path):
                continue
            
            with upload_lock_manager.acquire(entry):
                meta = load_metadata(entry)
                last_activity = None
                status = "unknown"
                if meta:
                    last_activity = meta.get("updated_at")
                    status = meta.get("status", "unknown")
                
                # Rule: Assembling uploads are strictly protected from normal cache cleanup
                if status == "assembling":
                    continue

                if last_activity is None:
                    try:
                        last_activity = os.path.getmtime(item_path)
                    except OSError:
                        continue

                if now - last_activity > UPLOAD_CACHE_TIMEOUT:
                    logger.info(f"UPLOAD EXPIRED: upload_id={entry} inactive for {int(now - last_activity)}s. Deleting cache.")
                    try:
                        shutil.rmtree(item_path, ignore_errors=True)
                        count_cleaned += 1
                    except Exception as err:
                        logger.error(f"Failed to remove expired cache for {entry}: {err}")
                        
        if count_cleaned > 0:
            logger.info(f"CACHE CLEANED: Removed {count_cleaned} expired upload cache(s).")
    except Exception as e:
        logger.error(f"Error during cache cleanup scan: {e}")

def cache_cleanup_worker():
    """Background thread worker to periodically clean expired caches."""
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL)
            clean_expired_cache()
        except Exception as e:
            logger.error(f"Exception in cache cleanup worker: {e}")

# Run initial cleanup on startup
clean_expired_cache()

# Start background cleanup thread safely with Flask debug reloader
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    cleanup_thread = threading.Thread(target=cache_cleanup_worker, daemon=True, name="CacheCleanupWorker")
    cleanup_thread.start()

# -----------------------------------------------------------------------------
# Frontend HTML / UI Template
# -----------------------------------------------------------------------------
UPLOAD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QuickShare — Fast & Resumable File Sharing</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --surface-color: #1e293b;
            --surface-hover: #334155;
            --card-border: #334155;
            --primary: #38bdf8;
            --primary-hover: #0ea5e9;
            --primary-light: rgba(56, 189, 248, 0.12);
            --success: #10b981;
            --success-light: rgba(16, 185, 129, 0.12);
            --danger: #ef4444;
            --danger-hover: #dc2626;
            --danger-light: rgba(239, 68, 68, 0.12);
            --warning: #f59e0b;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --border-radius: 12px;
            --transition: all 0.2s ease-in-out;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, "Open Sans", "Helvetica Neue", sans-serif;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2.5rem 1rem;
        }

        .container {
            width: 100%;
            max-width: 860px;
            display: flex;
            flex-direction: column;
            gap: 2rem;
        }

        header {
            text-align: center;
            margin-bottom: 0.5rem;
        }

        header h1 {
            font-size: 2.4rem;
            font-weight: 800;
            letter-spacing: -0.02em;
            background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.35rem;
        }

        header p {
            color: var(--text-secondary);
            font-size: 1rem;
        }

        .card {
            background-color: var(--surface-color);
            border: 1px solid var(--card-border);
            border-radius: var(--border-radius);
            padding: 1.75rem;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3), 0 8px 10px -6px rgba(0, 0, 0, 0.3);
        }

        .card-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: 1.25rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            color: var(--text-primary);
        }

        /* Dropzone */
        .dropzone {
            border: 2px dashed var(--card-border);
            border-radius: var(--border-radius);
            padding: 2.5rem 1.5rem;
            text-align: center;
            cursor: pointer;
            transition: var(--transition);
            background: rgba(15, 23, 42, 0.4);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 0.75rem;
        }

        .dropzone:hover, .dropzone.dragover {
            border-color: var(--primary);
            background: var(--primary-light);
        }

        .dropzone-icon {
            font-size: 2.5rem;
            color: var(--primary);
        }

        .dropzone-text {
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .dropzone-subtext {
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .file-input {
            display: none;
        }

        .btn {
            background-color: var(--primary);
            color: #0f172a;
            border: none;
            border-radius: 8px;
            padding: 0.65rem 1.25rem;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: var(--transition);
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            text-decoration: none;
        }

        .btn:hover {
            background-color: var(--primary-hover);
        }

        .btn-danger {
            background-color: var(--danger);
            color: #ffffff;
        }

        .btn-danger:hover {
            background-color: var(--danger-hover);
        }

        .btn-secondary {
            background-color: var(--surface-hover);
            color: var(--text-primary);
        }

        .btn-secondary:hover {
            background-color: #475569;
        }

        .btn-sm {
            padding: 0.4rem 0.75rem;
            font-size: 0.85rem;
            border-radius: 6px;
        }

        /* Upload Queue / Cards */
        .upload-queue {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            margin-top: 1.5rem;
        }

        .upload-item {
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid var(--card-border);
            border-radius: 10px;
            padding: 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            transition: var(--transition);
        }

        .upload-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
        }

        .upload-info {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
            overflow: hidden;
        }

        .upload-filename {
            font-weight: 600;
            font-size: 0.95rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 450px;
        }

        .upload-meta {
            font-size: 0.8rem;
            color: var(--text-secondary);
        }

        .upload-actions {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-shrink: 0;
        }

        .badge {
            font-size: 0.75rem;
            font-weight: 600;
            padding: 0.25rem 0.5rem;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .badge-uploading { background: var(--primary-light); color: var(--primary); }
        .badge-assembling { background: rgba(245, 158, 11, 0.15); color: var(--warning); }
        .badge-completed { background: var(--success-light); color: var(--success); }
        .badge-cancelled { background: var(--danger-light); color: var(--danger); }
        .badge-error { background: var(--danger-light); color: var(--danger); }
        .badge-paused { background: var(--surface-hover); color: var(--text-secondary); }

        .progress-bar-container {
            width: 100%;
            height: 8px;
            background-color: rgba(51, 65, 85, 0.6);
            border-radius: 9999px;
            overflow: hidden;
        }

        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #38bdf8, #818cf8);
            border-radius: 9999px;
            width: 0%;
            transition: width 0.15s ease-out;
        }

        .progress-bar-fill.completed {
            background: #10b981;
        }

        .upload-stats {
            display: flex;
            justify-content: space-between;
            font-size: 0.8rem;
            color: var(--text-secondary);
        }

        /* Resumable Session Alert */
        .resume-alert {
            background: rgba(56, 189, 248, 0.08);
            border: 1px solid rgba(56, 189, 248, 0.3);
            border-radius: 10px;
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1rem;
        }

        .resume-alert-text {
            font-size: 0.9rem;
            color: var(--text-primary);
        }

        /* File List */
        .file-list-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }

        .search-input {
            background-color: rgba(15, 23, 42, 0.6);
            border: 1px solid var(--card-border);
            border-radius: 8px;
            padding: 0.5rem 0.85rem;
            color: var(--text-primary);
            font-size: 0.85rem;
            outline: none;
            width: 200px;
            transition: var(--transition);
        }

        .search-input:focus {
            border-color: var(--primary);
            width: 250px;
        }

        .file-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }

        .file-table th {
            text-align: left;
            padding: 0.75rem 1rem;
            color: var(--text-muted);
            border-bottom: 1px solid var(--card-border);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .file-table td {
            padding: 0.9rem 1rem;
            border-bottom: 1px solid rgba(51, 65, 85, 0.4);
            vertical-align: middle;
        }

        .file-table tr:hover td {
            background-color: rgba(51, 65, 85, 0.2);
        }

        .file-name-cell {
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            word-break: break-all;
        }

        .empty-state {
            text-align: center;
            padding: 2.5rem 1rem;
            color: var(--text-muted);
            font-size: 0.95rem;
        }

        @media (max-width: 640px) {
            body { padding: 1.5rem 0.75rem; }
            .card { padding: 1.25rem; }
            .upload-header { flex-direction: column; }
            .upload-actions { width: 100%; justify-content: flex-end; }
            .file-table th:nth-child(3), .file-table td:nth-child(3) { display: none; }
            .search-input { width: 100%; }
            .file-list-header { flex-direction: column; align-items: stretch; gap: 0.75rem; }
        }
    </style>
</head>
<body>

<div class="container">
    <header>
        <h1>QuickShare</h1>
        <p>Reliable, chunked, and resumable file transfers</p>
    </header>

    <!-- Upload Card -->
    <div class="card">
        <div class="card-title">
            <span>Upload Files</span>
        </div>

        <div id="resumeContainer"></div>

        <div class="dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
            <div class="dropzone-icon">📁</div>
            <div class="dropzone-text">Click to choose files or drag & drop here</div>
            <div class="dropzone-subtext">Supports files of any size with automatic chunking & resume</div>
            <input type="file" id="fileInput" class="file-input" multiple onchange="handleFileSelection(event)">
        </div>

        <div class="upload-queue" id="uploadQueue"></div>
    </div>

    <!-- Completed Downloads Card -->
    <div class="card">
        <div class="file-list-header">
            <div class="card-title" style="margin-bottom: 0;">
                <span>Available Files (<span id="fileCount">{{ files|length }}</span>)</span>
            </div>
            {% if files %}
            <input type="text" id="fileSearch" class="search-input" placeholder="Search files..." oninput="filterFiles()">
            {% endif %}
        </div>

        {% if files %}
        <table class="file-table" id="fileTable">
            <thead>
                <tr>
                    <th>Filename</th>
                    <th>Size</th>
                    <th>Uploaded</th>
                    <th style="text-align: right;">Action</th>
                </tr>
            </thead>
            <tbody>
                {% for f in files %}
                <tr class="file-row">
                    <td>
                        <div class="file-name-cell">
                            <span>📄</span>
                            <span class="file-name-text">{{ f.name }}</span>
                        </div>
                    </td>
                    <td style="color: var(--text-secondary); white-space: nowrap;">{{ f.size_str }}</td>
                    <td style="color: var(--text-muted); white-space: nowrap;">{{ f.mtime_str }}</td>
                    <td style="text-align: right;">
                        <a href="/download/{{ f.name }}" class="btn btn-sm" download>Download</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">
            No files available for download yet. Upload a file above!
        </div>
        {% endif %}
    </div>
</div>

<script>
// ---------------------------------------------------------------------------
// Client Configuration
// ---------------------------------------------------------------------------
const CHUNK_SIZE = 5 * 1024 * 1024; // 5 MB
const UPLOAD_CONCURRENCY = 3;       // 3 concurrent chunks per file
const MAX_CHUNK_RETRIES = 5;        // Max retry attempts per chunk
const STORAGE_KEY = "quickshare_active_uploads";

// State
let activeUploaders = new Map();

// Helper: Format bytes
function formatBytes(bytes) {
    if (bytes === 0 || !bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Helper: Format seconds to ETA string
function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '--';
    if (seconds < 60) return Math.round(seconds) + 's';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m + 'm ' + s + 's';
}

// Helper: Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Local Storage Session Management
// ---------------------------------------------------------------------------
function getSavedUploads() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : {};
    } catch(e) {
        return {};
    }
}

function saveUploadSession(uploadId, data) {
    try {
        const sessions = getSavedUploads();
        sessions[uploadId] = data;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
    } catch(e) {}
}

function removeUploadSession(uploadId) {
    try {
        const sessions = getSavedUploads();
        delete sessions[uploadId];
        localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
    } catch(e) {}
}

// Check for interrupted uploads on load
function checkSavedSessions() {
    const sessions = getSavedUploads();
    const resumeContainer = document.getElementById("resumeContainer");
    resumeContainer.innerHTML = "";

    const sessionKeys = Object.keys(sessions);
    if (sessionKeys.length === 0) return;

    sessionKeys.forEach(uploadId => {
        const session = sessions[uploadId];
        const banner = document.createElement("div");
        banner.className = "resume-alert";
        banner.id = `resume-alert-${uploadId}`;
        banner.innerHTML = `
            <div class="resume-alert-text">
                ⚠️ <strong>Interrupted upload found:</strong> ${escapeHtml(session.filename)} (${formatBytes(session.total_size)})
            </div>
            <div style="display: flex; gap: 0.5rem;">
                <label class="btn btn-sm" style="cursor: pointer;">
                    Resume
                    <input type="file" style="display:none;" onchange="resumeSessionFile(event, '${uploadId}')">
                </label>
                <button class="btn btn-sm btn-secondary" onclick="dismissSavedSession('${uploadId}')">Dismiss</button>
            </div>
        `;
        resumeContainer.appendChild(banner);
    });
}

function dismissSavedSession(uploadId) {
    removeUploadSession(uploadId);
    const alert = document.getElementById(`resume-alert-${uploadId}`);
    if (alert) alert.remove();
}

function resumeSessionFile(event, uploadId) {
    const file = event.target.files[0];
    if (!file) return;

    const sessions = getSavedUploads();
    const session = sessions[uploadId];
    if (!session) return;

    // Strict identity check: name, size, and lastModified
    if (file.name !== session.filename || file.size !== session.total_size || (session.last_modified && file.lastModified !== session.last_modified)) {
        alert("The selected file does not match the interrupted upload ('" + session.filename + "'). Please select the exact file.");
        return;
    }

    dismissSavedSession(uploadId);
    startChunkedUpload(file, uploadId);
}

// ---------------------------------------------------------------------------
// Chunked Uploader Implementation
// ---------------------------------------------------------------------------
class ChunkedUploader {
    constructor(file, existingUploadId = null) {
        this.file = file;
        this.filename = file.name;
        this.totalSize = file.size;
        this.lastModified = file.lastModified;
        this.chunkSize = CHUNK_SIZE;
        this.totalChunks = Math.ceil(this.totalSize / this.chunkSize) || 1;
        this.uploadId = existingUploadId;
        
        this.receivedChunks = new Set();
        this.status = 'initializing'; // initializing, uploading, paused, assembling, completed, cancelled, error
        this.errorMessage = '';
        
        // Loop generation counter to prevent duplicate concurrent loops
        this.uploadGeneration = 0;
        this.isLoopRunning = false;
        
        // Progress & Smoothed Speed tracking
        this.uploadedBytes = 0;
        this.lastSpeedCheck = Date.now();
        this.bytesSinceLastCheck = 0;
        this.currentSpeed = 0; // Bytes/sec
        
        // Abort controllers for all active chunk requests
        this.abortControllers = new Map();
        
        // UI element ID
        this.elementId = 'upload-' + (this.uploadId || 'temp-' + Math.random().toString(36).substr(2, 9));
    }

    renderCard() {
        const queue = document.getElementById("uploadQueue");
        let card = document.getElementById(this.elementId);
        if (!card) {
            card = document.createElement("div");
            card.className = "upload-item";
            card.id = this.elementId;
            queue.prepend(card);
        }

        card.innerHTML = `
            <div class="upload-header">
                <div class="upload-info">
                    <div class="upload-filename" title="${escapeHtml(this.filename)}">${escapeHtml(this.filename)}</div>
                    <div class="upload-meta">${formatBytes(this.totalSize)} • <span id="${this.elementId}-chunks">0/${this.totalChunks} chunks</span></div>
                </div>
                <div class="upload-actions">
                    <span class="badge badge-uploading" id="${this.elementId}-badge">Uploading</span>
                    <button class="btn btn-sm btn-secondary" id="${this.elementId}-pause-btn" onclick="togglePauseUpload('${this.elementId}')">Pause</button>
                    <button class="btn btn-sm btn-danger" id="${this.elementId}-cancel-btn" onclick="cancelUpload('${this.elementId}')">Cancel</button>
                </div>
            </div>
            <div class="progress-bar-container">
                <div class="progress-bar-fill" id="${this.elementId}-fill" style="width: 0%"></div>
            </div>
            <div class="upload-stats">
                <span id="${this.elementId}-progress-text">0% • 0 B / ${formatBytes(this.totalSize)}</span>
                <span id="${this.elementId}-speed-text">-- KB/s • ETA: --</span>
            </div>
        `;
    }

    updateUI() {
        const card = document.getElementById(this.elementId);
        if (!card) return;

        const badge = document.getElementById(`${this.elementId}-badge`);
        const fill = document.getElementById(`${this.elementId}-fill`);
        const progressText = document.getElementById(`${this.elementId}-progress-text`);
        const speedText = document.getElementById(`${this.elementId}-speed-text`);
        const chunksText = document.getElementById(`${this.elementId}-chunks`);
        const pauseBtn = document.getElementById(`${this.elementId}-pause-btn`);
        const cancelBtn = document.getElementById(`${this.elementId}-cancel-btn`);

        if (chunksText) {
            chunksText.textContent = `${this.receivedChunks.size}/${this.totalChunks} chunks`;
        }

        // Calculate progress percentage based on confirmed chunks
        let percent = 0;
        if (this.totalSize > 0) {
            let confirmedBytes = 0;
            for (let idx of this.receivedChunks) {
                const start = idx * this.chunkSize;
                const end = Math.min(start + this.chunkSize, this.totalSize);
                confirmedBytes += (end - start);
            }
            this.uploadedBytes = confirmedBytes;
            percent = Math.min(99, Math.round((confirmedBytes / this.totalSize) * 100));
        } else if (this.totalSize === 0 && this.receivedChunks.size > 0) {
            percent = (this.status === 'completed') ? 100 : 99;
        }

        if (this.status === 'completed') {
            percent = 100;
        }

        if (fill) {
            fill.style.width = `${percent}%`;
            if (this.status === 'completed') fill.classList.add('completed');
        }

        if (progressText) {
            progressText.textContent = `${percent}% • ${formatBytes(this.uploadedBytes)} / ${formatBytes(this.totalSize)}`;
        }

        // Update ETA & Speed
        if (speedText) {
            if (this.status === 'uploading') {
                const remainingBytes = Math.max(0, this.totalSize - this.uploadedBytes);
                const etaSeconds = this.currentSpeed > 0 ? (remainingBytes / this.currentSpeed) : 0;
                speedText.textContent = `${formatBytes(this.currentSpeed)}/s • ETA: ${formatTime(etaSeconds)}`;
            } else if (this.status === 'assembling') {
                speedText.textContent = `Verifying & assembling final file...`;
            } else if (this.status === 'completed') {
                speedText.textContent = `Upload verified and complete!`;
            } else if (this.status === 'cancelled') {
                speedText.textContent = `Upload cancelled`;
            } else if (this.status === 'paused') {
                speedText.textContent = `Upload paused`;
            } else if (this.status === 'error') {
                speedText.textContent = `Error: ${this.errorMessage}`;
            }
        }

        // Update Badges & Buttons
        if (badge) {
            badge.className = `badge badge-${this.status}`;
            badge.textContent = this.status.toUpperCase();
        }

        if (pauseBtn) {
            if (this.status === 'completed' || this.status === 'cancelled' || this.status === 'error' || this.status === 'assembling') {
                pauseBtn.style.display = 'none';
            } else {
                pauseBtn.style.display = 'inline-block';
                pauseBtn.textContent = (this.status === 'paused') ? 'Resume' : 'Pause';
            }
        }

        if (cancelBtn) {
            if (this.status === 'completed' || this.status === 'cancelled') {
                cancelBtn.style.display = 'none';
            } else {
                cancelBtn.style.display = 'inline-block';
            }
        }
    }

    async start() {
        this.renderCard();
        this.status = 'uploading';
        this.updateUI();

        try {
            // Step 1: Start or Recover upload session
            if (!this.uploadId) {
                const startRes = await fetch('/upload/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        filename: this.filename,
                        total_size: this.totalSize,
                        chunk_size: this.chunkSize
                    })
                });
                
                const startData = await startRes.json();
                if (!startData.success) {
                    throw new Error(startData.error || "Failed to start upload session");
                }
                
                this.uploadId = startData.upload_id;
                this.chunkSize = startData.chunk_size;
                this.totalChunks = startData.total_chunks;
            } else {
                const statusRes = await fetch(`/upload/status/${this.uploadId}`);
                if (!statusRes.ok) {
                    const errData = await statusRes.json().catch(() => ({}));
                    throw new Error(errData.error || "Upload session expired or not found");
                }
                const statusData = await statusRes.json();
                if (statusData.success) {
                    this.receivedChunks = new Set(statusData.received_chunks);
                    this.chunkSize = statusData.chunk_size;
                    this.totalChunks = statusData.total_chunks;
                } else {
                    throw new Error(statusData.error || "Upload session not found");
                }
            }

            // Save to localStorage for recovery
            saveUploadSession(this.uploadId, {
                upload_id: this.uploadId,
                filename: this.filename,
                total_size: this.totalSize,
                chunk_size: this.chunkSize,
                total_chunks: this.totalChunks,
                last_modified: this.lastModified
            });

            this.updateUI();

            // Step 2: Upload missing chunks concurrently
            await this.uploadChunksLoop();

            if (this.status === 'cancelled' || this.status === 'paused') return;

            // Step 3: Complete upload
            this.status = 'assembling';
            this.updateUI();

            const completeRes = await fetch('/upload/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: this.uploadId })
            });

            const completeData = await completeRes.json();
            if (!completeData.success) {
                throw new Error(completeData.error || "Failed to complete and verify upload");
            }

            this.status = 'completed';
            removeUploadSession(this.uploadId);
            this.updateUI();

            // Refresh file list after 1.2 seconds
            setTimeout(() => {
                window.location.reload();
            }, 1200);

        } catch (err) {
            if (this.status === 'cancelled' || this.status === 'paused') return;
            console.error("Upload error:", err);
            this.status = 'error';
            this.errorMessage = err.message || "Network error occurred";
            this.updateUI();
        }
    }

    async uploadChunksLoop() {
        if (this.isLoopRunning) return;
        this.isLoopRunning = true;
        this.uploadGeneration++;
        const currentGen = this.uploadGeneration;

        try {
            const queue = [];
            for (let i = 0; i < this.totalChunks; i++) {
                if (!this.receivedChunks.has(i)) {
                    queue.push(i);
                }
            }

            let workerCount = Math.min(UPLOAD_CONCURRENCY, queue.length || 1);
            const workers = [];

            // Smoothed speed calculation timer
            const speedInterval = setInterval(() => {
                if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) {
                    clearInterval(speedInterval);
                    return;
                }
                const now = Date.now();
                const elapsed = (now - this.lastSpeedCheck) / 1000;
                if (elapsed > 0.5) {
                    const instantSpeed = this.bytesSinceLastCheck / elapsed;
                    this.currentSpeed = this.currentSpeed === 0 ? Math.round(instantSpeed) : Math.round(0.7 * instantSpeed + 0.3 * this.currentSpeed);
                    this.bytesSinceLastCheck = 0;
                    this.lastSpeedCheck = now;
                    this.updateUI();
                }
            }, 600);

            for (let w = 0; w < workerCount; w++) {
                workers.push((async () => {
                    while (queue.length > 0 && this.status === 'uploading' && this.uploadGeneration === currentGen) {
                        const chunkIndex = queue.shift();
                        let success = false;
                        let retries = 0;

                        while (!success && retries < MAX_CHUNK_RETRIES && this.status === 'uploading' && this.uploadGeneration === currentGen) {
                            try {
                                await this.uploadSingleChunk(chunkIndex, currentGen);
                                success = true;
                                this.receivedChunks.add(chunkIndex);
                                this.updateUI();
                            } catch (err) {
                                if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) break;
                                retries++;
                                console.warn(`Retry ${retries}/${MAX_CHUNK_RETRIES} for chunk ${chunkIndex}:`, err);
                                await new Promise(r => setTimeout(r, Math.min(800 * Math.pow(1.5, retries), 6000)));
                            }
                        }

                        if (!success && this.status === 'uploading' && this.uploadGeneration === currentGen) {
                            throw new Error(`Failed to upload chunk #${chunkIndex} after ${MAX_CHUNK_RETRIES} attempts`);
                        }
                    }
                })());
            }

            await Promise.all(workers);
            clearInterval(speedInterval);
        } finally {
            if (this.uploadGeneration === currentGen) {
                this.isLoopRunning = false;
            }
        }
    }

    async uploadSingleChunk(chunkIndex, currentGen) {
        if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) return;

        const start = chunkIndex * this.chunkSize;
        const end = Math.min(start + this.chunkSize, this.totalSize);
        const chunkBlob = (this.totalSize === 0) ? new Blob([]) : this.file.slice(start, end);
        const chunkSize = end - start;

        const formData = new FormData();
        formData.append('upload_id', this.uploadId);
        formData.append('chunk_index', chunkIndex);
        formData.append('total_chunks', this.totalChunks);
        formData.append('chunk', chunkBlob, `chunk_${chunkIndex}`);

        const controller = new AbortController();
        this.abortControllers.set(chunkIndex, controller);

        try {
            const response = await fetch('/upload/chunk', {
                method: 'POST',
                body: formData,
                signal: controller.signal
            });

            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || `Server returned HTTP ${response.status}`);
            }

            this.bytesSinceLastCheck += chunkSize;
        } finally {
            this.abortControllers.delete(chunkIndex);
        }
    }

    pause() {
        if (this.status !== 'uploading') return;
        this.status = 'paused';
        this.uploadGeneration++;
        this.isLoopRunning = false;

        // Abort in-flight requests
        for (let controller of this.abortControllers.values()) {
            controller.abort();
        }
        this.abortControllers.clear();
        this.updateUI();
    }

    resume() {
        if (this.status !== 'paused' && this.status !== 'error') return;
        this.status = 'uploading';
        this.errorMessage = '';
        this.lastSpeedCheck = Date.now();
        this.bytesSinceLastCheck = 0;
        this.updateUI();

        // Query server to get actual missing chunks
        fetch(`/upload/status/${this.uploadId}`).then(res => {
            if (!res.ok) {
                return res.json().then(e => { throw new Error(e.error || "Upload expired or not found on server"); });
            }
            return res.json();
        }).then(statusData => {
            if (this.status !== 'uploading') return;
            if (statusData.success) {
                this.receivedChunks = new Set(statusData.received_chunks);
            }
            return this.uploadChunksLoop();
        }).then(async () => {
            if (this.status !== 'uploading') return;
            this.status = 'assembling';
            this.updateUI();

            const completeRes = await fetch('/upload/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: this.uploadId })
            });

            const completeData = await completeRes.json();
            if (!completeData.success) {
                throw new Error(completeData.error || "Failed to complete upload");
            }

            this.status = 'completed';
            removeUploadSession(this.uploadId);
            this.updateUI();
            setTimeout(() => location.reload(), 1200);
        }).catch(err => {
            if (this.status === 'cancelled' || this.status === 'paused') return;
            this.status = 'error';
            this.errorMessage = err.message || "Resume failed";
            this.updateUI();
        });
    }

    async cancel() {
        this.status = 'cancelled';
        this.uploadGeneration++;
        this.isLoopRunning = false;

        // Abort all active fetch requests immediately
        for (let controller of this.abortControllers.values()) {
            controller.abort();
        }
        this.abortControllers.clear();

        // Send cancellation to server to delete cache
        if (this.uploadId) {
            try {
                await fetch(`/upload/cancel/${this.uploadId}`, { method: 'POST' });
            } catch (e) {
                console.warn("Failed to notify server of cancellation:", e);
            }
            removeUploadSession(this.uploadId);
        }

        this.updateUI();
    }
}

// ---------------------------------------------------------------------------
// UI Event Handlers & Visibility Lifecycle
// ---------------------------------------------------------------------------
function handleFileSelection(event) {
    const files = event.target.files;
    if (!files || files.length === 0) return;

    for (let i = 0; i < files.length; i++) {
        startChunkedUpload(files[i]);
    }
    event.target.value = '';
}

function startChunkedUpload(file, existingUploadId = null) {
    const uploader = new ChunkedUploader(file, existingUploadId);
    activeUploaders.set(uploader.elementId, uploader);
    uploader.start();
}

function cancelUpload(elementId) {
    const uploader = activeUploaders.get(elementId);
    if (uploader) {
        uploader.cancel();
    }
}

function togglePauseUpload(elementId) {
    const uploader = activeUploaders.get(elementId);
    if (uploader) {
        if (uploader.status === 'paused') {
            uploader.resume();
        } else {
            uploader.pause();
        }
    }
}

// Drag & Drop
const dropzone = document.getElementById("dropzone");
['dragenter', 'dragover'].forEach(name => {
    dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add('dragover');
    });
});

['dragleave', 'drop'].forEach(name => {
    dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('dragover');
    });
});

dropzone.addEventListener('drop', (e) => {
    const dt = e.dataTransfer;
    const files = dt.files;
    if (files && files.length > 0) {
        for (let i = 0; i < files.length; i++) {
            startChunkedUpload(files[i]);
        }
    }
});

// File list search
function filterFiles() {
    const query = document.getElementById('fileSearch').value.toLowerCase();
    const rows = document.querySelectorAll('.file-row');
    let visible = 0;
    rows.forEach(row => {
        const name = row.querySelector('.file-name-text').textContent.toLowerCase();
        if (name.includes(query)) {
            row.style.display = '';
            visible++;
        } else {
            row.style.display = 'none';
        }
    });
    document.getElementById('fileCount').textContent = visible;
}

// Page visibility and connection recovery
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        activeUploaders.forEach(uploader => {
            if (uploader.status === 'uploading' && !uploader.isLoopRunning) {
                // Resume loop safely if workers were suspended by browser throttling
                uploader.uploadChunksLoop();
            } else if (uploader.status === 'uploading') {
                uploader.updateUI();
            }
        });
    }
});

window.addEventListener('online', () => {
    console.info("Network online detected: reconciling active uploads");
    activeUploaders.forEach(uploader => {
        if (uploader.status === 'uploading' || uploader.status === 'error') {
            uploader.resume();
        }
    });
});

// Init
window.addEventListener('DOMContentLoaded', () => {
    checkSavedSessions();
});
</script>

</body>
</html>
"""

# -----------------------------------------------------------------------------
# REST API Endpoints
# -----------------------------------------------------------------------------

@app.route('/')
def index():
    """Renders the main QuickShare page listing completed files in uploads/."""
    file_list = []
    if os.path.exists(UPLOAD_DIR):
        try:
            for fname in os.listdir(UPLOAD_DIR):
                full_path = os.path.join(UPLOAD_DIR, fname)
                if os.path.isfile(full_path) and not fname.startswith('.') and is_safe_path(UPLOAD_DIR, full_path):
                    stat = os.stat(full_path)
                    file_list.append({
                        "name": fname,
                        "size": stat.st_size,
                        "size_str": format_bytes(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    })
            file_list.sort(key=lambda x: x["mtime"], reverse=True)
        except Exception as e:
            logger.error(f"Error listing uploads directory: {e}")
            
    return render_template_string(UPLOAD_HTML, files=file_list)


@app.route('/upload/start', methods=['POST'])
def upload_start():
    """Initializes a new upload session, creates cache/<upload_id>/ and metadata.json."""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "").strip()
    total_size = data.get("total_size")
    chunk_size = data.get("chunk_size", DEFAULT_CHUNK_SIZE)
    file_hash = data.get("file_hash", "")

    if not filename:
        return jsonify({"success": False, "error": "Filename is required"}), 400

    try:
        total_size = int(total_size)
        if total_size < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Valid total_size integer is required"}), 400

    try:
        chunk_size = int(chunk_size)
        if chunk_size <= 0:
            chunk_size = DEFAULT_CHUNK_SIZE
    except (TypeError, ValueError):
        chunk_size = DEFAULT_CHUNK_SIZE

    total_chunks = math.ceil(total_size / chunk_size) if total_size > 0 else 1
    upload_id = str(uuid.uuid4())
    safe_name = sanitize_filename(filename)

    cache_dir = get_upload_cache_dir(upload_id)
    chunks_dir = get_chunks_dir(upload_id)
    os.makedirs(chunks_dir, exist_ok=True)

    metadata = {
        "upload_id": upload_id,
        "filename": filename,
        "safe_filename": safe_name,
        "total_size": total_size,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "received_chunks": [],
        "file_hash": file_hash,
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "uploading"
    }

    with upload_lock_manager.acquire(upload_id):
        if not save_metadata(upload_id, metadata):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "Failed to initialize upload metadata"}), 500

    logger.info(f"UPLOAD START: upload_id={upload_id}, file='{safe_name}', size={total_size} bytes ({total_chunks} chunks of {chunk_size} B)")
    return jsonify({
        "success": True,
        "upload_id": upload_id,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "status": "uploading"
    }), 201


@app.route('/upload/chunk', methods=['POST'])
def upload_chunk():
    """Receives and securely stores a single chunk for an upload session."""
    upload_id = request.form.get("upload_id")
    chunk_index_raw = request.form.get("chunk_index")
    total_chunks_raw = request.form.get("total_chunks")
    chunk_file = request.files.get("chunk")

    if not upload_id or not is_valid_uuid(upload_id):
        return jsonify({"success": False, "error": "Invalid or missing upload_id"}), 400

    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir or not os.path.exists(cache_dir):
        return jsonify({"success": False, "error": "Upload session not found or expired"}), 404

    with upload_lock_manager.acquire(upload_id):
        metadata = load_metadata(upload_id)
        if not metadata:
            return jsonify({"success": False, "error": "Upload session metadata not found"}), 404

        current_status = metadata.get("status")
        # Strict state validation: if already assembling, reject with 409 Conflict
        if current_status == "assembling":
            return jsonify({"success": False, "error": "Upload assembly is already in progress"}), 409

        if current_status in ["cancelled", "completed", "failed"]:
            return jsonify({"success": False, "error": f"Upload session is {current_status}"}), 400

        try:
            chunk_index = int(chunk_index_raw)
            total_chunks = int(total_chunks_raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Invalid chunk_index or total_chunks"}), 400

        if total_chunks != metadata["total_chunks"]:
            return jsonify({"success": False, "error": "total_chunks mismatch with upload session"}), 400

        if chunk_index < 0 or chunk_index >= metadata["total_chunks"]:
            return jsonify({"success": False, "error": f"chunk_index out of bounds (0..{metadata['total_chunks'] - 1})"}), 400

        if not chunk_file:
            return jsonify({"success": False, "error": "Missing chunk file payload"}), 400

        # Authoritative server-side expected chunk size calculation
        expected_start = chunk_index * metadata["chunk_size"]
        expected_end = min(expected_start + metadata["chunk_size"], metadata["total_size"])
        expected_chunk_size = max(0, expected_end - expected_start)

        chunks_dir = get_chunks_dir(upload_id)
        os.makedirs(chunks_dir, exist_ok=True)
        chunk_filename = f"{chunk_index:06d}"
        chunk_path = os.path.join(chunks_dir, chunk_filename)
        temp_chunk_path = f"{chunk_path}.tmp_{uuid.uuid4().hex[:6]}"

        try:
            chunk_file.save(temp_chunk_path)
            actual_chunk_size = os.path.getsize(temp_chunk_path)

            if actual_chunk_size != expected_chunk_size:
                os.remove(temp_chunk_path)
                return jsonify({
                    "success": False,
                    "error": f"Chunk size mismatch: expected {expected_chunk_size} B, got {actual_chunk_size} B"
                }), 400

            # Atomic replace for chunk file (idempotent overwrite if retried)
            os.replace(temp_chunk_path, chunk_path)

            # Update metadata
            metadata["received_chunks"].add(chunk_index)
            metadata["updated_at"] = time.time()
            if not save_metadata(upload_id, metadata):
                return jsonify({"success": False, "error": "Failed to persist upload metadata"}), 500

        except Exception as e:
            if os.path.exists(temp_chunk_path):
                try:
                    os.remove(temp_chunk_path)
                except OSError:
                    pass
            logger.error(f"Error saving chunk {chunk_index} for upload_id={upload_id}: {e}")
            return jsonify({"success": False, "error": "Internal error storing chunk"}), 500

    logger.info(f"CHUNK RECEIVED: upload_id={upload_id}, chunk={chunk_index}/{metadata['total_chunks']-1}, received_total={len(metadata['received_chunks'])}")
    return jsonify({
        "success": True,
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "received_count": len(metadata["received_chunks"])
    }), 200


@app.route('/upload/status/<path:upload_id>', methods=['GET'])
def upload_status(upload_id):
    """Returns the current authoritative state and missing chunks of an upload session."""
    if not is_valid_uuid(upload_id):
        return jsonify({"success": False, "error": "Invalid upload_id format"}), 400

    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir or not os.path.exists(cache_dir):
        return jsonify({"success": False, "error": "Upload session not found or expired"}), 404

    with upload_lock_manager.acquire(upload_id):
        metadata = load_metadata(upload_id)
        if not metadata:
            return jsonify({"success": False, "error": "Upload session metadata not found"}), 404

        total_chunks = metadata["total_chunks"]
        chunks_dir = get_chunks_dir(upload_id)
        actual_chunks = set()
        if os.path.exists(chunks_dir):
            for fname in os.listdir(chunks_dir):
                if fname.isdigit():
                    idx = int(fname)
                    if 0 <= idx < total_chunks:
                        actual_chunks.add(idx)

        metadata["received_chunks"] = actual_chunks
        metadata["updated_at"] = time.time()
        save_metadata(upload_id, metadata)

        received_list = sorted(list(actual_chunks))
        missing_chunks = [i for i in range(total_chunks) if i not in actual_chunks]
        next_chunk = missing_chunks[0] if missing_chunks else None

    logger.info(f"UPLOAD RESUMED/STATUS: upload_id={upload_id}, received={len(received_list)}/{total_chunks}, missing={len(missing_chunks)}")
    return jsonify({
        "success": True,
        "upload_id": upload_id,
        "filename": metadata["filename"],
        "safe_filename": metadata["safe_filename"],
        "total_size": metadata["total_size"],
        "chunk_size": metadata["chunk_size"],
        "total_chunks": total_chunks,
        "received_chunks": received_list,
        "missing_chunks": missing_chunks,
        "next_chunk": next_chunk,
        "status": metadata.get("status", "uploading")
    }), 200


@app.route('/upload/complete', methods=['POST'])
def upload_complete():
    """Three-phase non-blocking assembly: validates state, streams chunks, and atomically finalizes."""
    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id")

    if not upload_id or not is_valid_uuid(upload_id):
        return jsonify({"success": False, "error": "Invalid or missing upload_id"}), 400

    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir or not os.path.exists(cache_dir):
        return jsonify({"success": False, "error": "Upload session not found or already completed"}), 404

    # -------------------------------------------------------------------------
    # PHASE 1: Validation and Status Transition (Holding Upload Lock)
    # -------------------------------------------------------------------------
    with upload_lock_manager.acquire(upload_id):
        metadata = load_metadata(upload_id)
        if not metadata:
            return jsonify({"success": False, "error": "Upload metadata not found"}), 404

        current_status = metadata.get("status")
        if current_status == "completed":
            return jsonify({"success": True, "message": "Upload already completed", "filename": metadata.get("safe_filename")}), 200

        if current_status == "cancelled":
            return jsonify({"success": False, "error": "Upload was cancelled"}), 400

        if current_status == "assembling":
            return jsonify({"success": False, "error": "Assembly already in progress"}), 409

        chunks_dir = get_chunks_dir(upload_id)
        total_chunks = metadata["total_chunks"]
        expected_size = metadata["total_size"]

        # Verify all expected chunk files exist on disk
        missing_chunks = []
        for i in range(total_chunks):
            chunk_path = os.path.join(chunks_dir, f"{i:06d}")
            if not os.path.exists(chunk_path):
                missing_chunks.append(i)

        if missing_chunks:
            return jsonify({
                "success": False,
                "error": f"Cannot assemble upload. Missing {len(missing_chunks)} chunks",
                "missing_chunks": missing_chunks
            }), 400

        # Transition status to assembling
        metadata["status"] = "assembling"
        metadata["updated_at"] = time.time()
        save_metadata(upload_id, metadata)
        logger.info(f"ASSEMBLY START: upload_id={upload_id}, total_chunks={total_chunks}, expected_size={expected_size}")

    # -------------------------------------------------------------------------
    # PHASE 2: Streaming Assembly & Hashing (NO LOCK HELD - NON-BLOCKING)
    # -------------------------------------------------------------------------
    assembled_tmp = os.path.join(cache_dir, "assembled.tmp")
    hasher = hashlib.sha256()
    assembled_size = 0
    assembly_error = None

    try:
        with open(assembled_tmp, "wb") as outfile:
            for i in range(total_chunks):
                chunk_path = os.path.join(chunks_dir, f"{i:06d}")
                with open(chunk_path, "rb") as infile:
                    while True:
                        buffer = infile.read(64 * 1024)  # 64 KB streaming buffer
                        if not buffer:
                            break
                        outfile.write(buffer)
                        hasher.update(buffer)
                        assembled_size += len(buffer)
                outfile.flush()

        if assembled_size != expected_size:
            assembly_error = f"Assembled file size verification failed (expected {expected_size} B, got {assembled_size} B)"
            logger.error(f"SIZE VERIFICATION FAILURE: upload_id={upload_id} {assembly_error}")
        else:
            client_hash = metadata.get("file_hash")
            calculated_hash = hasher.hexdigest()
            if client_hash and client_hash.lower() != calculated_hash.lower():
                assembly_error = f"File SHA-256 integrity verification failed ({calculated_hash} != {client_hash})"
                logger.error(f"SHA-256 FAILURE: upload_id={upload_id} {assembly_error}")

    except Exception as e:
        assembly_error = f"Assembly streaming failed: {str(e)}"
        logger.error(f"ASSEMBLY FAILED: upload_id={upload_id} {assembly_error}")

    # -------------------------------------------------------------------------
    # PHASE 3: Re-verify State, Handle Cancel Race, & Finalize (Holding Lock)
    # -------------------------------------------------------------------------
    with upload_lock_manager.acquire(upload_id):
        latest_meta = load_metadata(upload_id)
        if not latest_meta or latest_meta.get("status") == "cancelled":
            if os.path.exists(assembled_tmp):
                try:
                    os.remove(assembled_tmp)
                except OSError:
                    pass
            shutil.rmtree(cache_dir, ignore_errors=True)
            logger.info(f"CANCEL WON RACE: upload_id={upload_id} cancelled during assembly. Purged.")
            return jsonify({"success": False, "error": "Upload was cancelled during assembly"}), 400

        if assembly_error:
            if os.path.exists(assembled_tmp):
                try:
                    os.remove(assembled_tmp)
                except OSError:
                    pass
            latest_meta["status"] = "failed"
            latest_meta["updated_at"] = time.time()
            save_metadata(upload_id, latest_meta)
            return jsonify({"success": False, "error": assembly_error}), 400

        # Atomic collision-safe final filename generation
        with upload_lock_manager.filename_lock():
            final_filename = get_unique_filename(UPLOAD_DIR, latest_meta["safe_filename"])
            final_dest = os.path.join(UPLOAD_DIR, final_filename)
            shutil.move(assembled_tmp, final_dest)

        latest_meta["status"] = "completed"
        latest_meta["safe_filename"] = final_filename
        latest_meta["updated_at"] = time.time()
        save_metadata(upload_id, latest_meta)

        shutil.rmtree(cache_dir, ignore_errors=True)

    calculated_hash = hasher.hexdigest()
    logger.info(f"UPLOAD COMPLETED: upload_id={upload_id}, saved='{final_filename}', size={assembled_size} bytes, sha256={calculated_hash}")
    return jsonify({
        "success": True,
        "filename": final_filename,
        "size": assembled_size,
        "sha256": calculated_hash,
        "message": f"{final_filename} uploaded and verified successfully"
    }), 200


@app.route('/upload/cancel/<path:upload_id>', methods=['POST', 'DELETE'])
@app.route('/upload/<path:upload_id>', methods=['DELETE'])
def upload_cancel(upload_id):
    """Explicitly cancels an in-progress upload and completely purges its cache."""
    if not is_valid_uuid(upload_id):
        return jsonify({"success": False, "error": "Invalid upload_id format"}), 400

    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir:
        return jsonify({"success": False, "error": "Invalid upload path"}), 400

    with upload_lock_manager.acquire(upload_id):
        if os.path.exists(cache_dir):
            try:
                meta = load_metadata(upload_id)
                if meta:
                    meta["status"] = "cancelled"
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta)

                shutil.rmtree(cache_dir, ignore_errors=True)
                logger.info(f"UPLOAD CANCELLED: upload_id={upload_id}. Entire cache deleted.")
                return jsonify({"success": True, "message": "Upload cancelled and cache purged successfully"}), 200
            except Exception as e:
                logger.error(f"Error purging cache during cancel for upload_id={upload_id}: {e}")
                return jsonify({"success": False, "error": "Failed to purge cache"}), 500
        else:
            return jsonify({"success": True, "message": "Upload already cancelled or not found"}), 200


@app.route('/download/<path:filename>')
def download_file(filename):
    """Securely serves completed files strictly from uploads/."""
    safe_name = os.path.basename(filename)
    target_path = os.path.join(UPLOAD_DIR, safe_name)
    
    if not is_safe_path(UPLOAD_DIR, target_path):
        abort(403)
        
    if not os.path.exists(target_path) or not os.path.isfile(target_path):
        abort(404)
        
    return send_from_directory(UPLOAD_DIR, safe_name, as_attachment=True)


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=5000)