# QuickShare

**QuickShare** is a lightweight, reliable, chunked, and resumable local area network (LAN) file-sharing application. It utilizes a Python Flask coordination server to facilitate fast and safe transfers between devices (e.g., PC to phone, laptop to desktop) over local Wi-Fi / Ethernet without file corruption or partial uploads.

---

## 1. Project Overview
QuickShare enables cross-device file sharing across local networks by coordinating transfers through an isolated staging cache (`cache/<upload_id>/`), streaming file assembly, SHA-256 integrity verification, and atomic file finalization into `uploads/`.

```
[ Device A (Browser / Client) ]
               │
               ▼ (HTTP Chunked Uploads)
[ Flask QuickShare Server ] ──► Staging (cache/<upload_id>/) ──► Verified (uploads/)
               │
               ▼ (HTTP Download)
[ Device B (Browser / Client) ]
```

---

## 2. Key Features
- **Chunked Uploads**: Slices large files into manageable chunks (default: 5 MB) via browser `File.slice()` to minimize memory footprint and request payload limits.
- **Resumable Transfers**: Interrupted transfers resume from the exact missing chunk without re-uploading completed chunks from byte zero.
- **True Pause & Resume**: Manual pause immediately aborts in-flight network requests (`AbortController`) while safely preserving the server staging cache. Background events (`online`, `visibilitychange`) will **never** override an explicit manual pause.
- **Instant Cancellation**: Users can cancel an upload at any time; active network requests are aborted and server-side cache is deleted immediately.
- **SHA-256 File Integrity Verification**: Ensures the final assembled file matches the client's original file hash and exact byte size.
- **Controlled Concurrency**: 3 concurrent chunk workers per file (`UPLOAD_CONCURRENCY = 3`) for optimal network saturation.
- **Streaming Assembly**: Large files are assembled using a fixed 64 KB streaming buffer (`infile.read(64 * 1024)`), guaranteeing low memory consumption even for multi-gigabyte files.
- **Collision-Safe Filenames**: Automatically generates unique names (e.g., `movie (1).mp4`) without overwriting existing files in `uploads/`.
- **Cache Isolation**: In-progress chunks remain strictly inside `cache/<upload_id>/` and are never accessible via the download endpoint until verified and finalized.
- **Automatic Cache Cleanup**: Background worker daemon safely purges abandoned incomplete uploads older than `UPLOAD_CACHE_TIMEOUT` (default: 6 hours), while strictly protecting active assemblies (`status == "assembling"`).
- **Browser Session Recovery**: Active upload sessions are tracked in browser `localStorage`. When the page is reloaded, an interrupted upload prompt allows instant resume upon file selection.
- **Network & Tab Visibility Recovery**: Automatically reconciles missing chunk state with the server when connection is restored or when returning to a backgrounded tab.
- **Per-Upload Locking (`UploadLockManager`)**: Independent uploads synchronize on their own isolated lock with reference counting, eliminating global thread contention.
- **Atomic File Operations**: Metadata files and assembled files use atomic file replacement (`os.replace`) to prevent corruption during sudden server stops.
- **Responsive Mobile-First UI**: Touch-friendly cards, fluid typography (`clamp`), no horizontal scrolling, and accessible progress notifications.

---

## 3. Architecture & Data Flow

```
[ User Selects File in Browser ]
               │
               ▼ (POST /upload/start)
[ Session Initialization ] ──► Creates cache/<upload_id>/metadata.json
               │
               ▼ (POST /upload/chunk)
[ Chunk Staging ] ───────────► Stores chunks in cache/<upload_id>/chunks/000000...
               │
               ▼ (POST /upload/complete)
[ 3-Phase Non-Blocking Assembly ]
   ├─ Phase 1: Validates chunks & state, transitions status to 'assembling' (locks upload)
   ├─ Phase 2: Streams chunks into cache/<upload_id>/assembled.tmp & computes SHA-256 (no lock)
   └─ Phase 3: Checks cancel race, collision-safe filename, atomic move to uploads/, purges cache
               │
               ▼ (GET /download/<filename>)
[ Secure Download Serving ] ─► Serves complete files strictly from uploads/
```

### Directory Structure
```
QuickShare/
├── .github/
│   └── workflows/
│       └── tests.yml      # GitHub Actions CI matrix workflow (Python 3.10, 3.11, 3.12)
├── Quickshare.py          # Flask application and embedded responsive frontend
├── requirements.txt       # Production dependencies
├── test_quickshare.py     # 28-scenario automated concurrency and reliability test suite
├── README.md              # Complete operational and technical manual
├── .gitignore             # Excludes uploads/, cache/, and bytecode
├── uploads/               # Verified completed files (auto-created on startup)
└── cache/                 # Temporary chunk staging directories (auto-created on startup)
```

---

## 4. Requirements
- **Python**: Python 3.8 or higher (Python 3.10+ recommended)
- **Dependencies**:
  - `Flask >= 3.0.0`
  - `Werkzeug >= 3.0.0`
- **Supported Platforms**: Windows, Linux, macOS
- **Browser Requirements**: Modern browser supporting `fetch`, `AbortController`, `File.slice()`, and `localStorage` (Chrome, Edge, Firefox, Safari).

---

## 5. Installation & Setup

### Windows (PowerShell)
```powershell
# 1. Clone repository
git clone https://github.com/SurendiranBJ/QuickShare.git
cd QuickShare

# 2. Create virtual environment
python -m venv venv

# 3. Activate virtual environment
.\venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements.txt
```

### Linux / macOS (Bash / Zsh)
```bash
# 1. Clone repository
git clone https://github.com/SurendiranBJ/QuickShare.git
cd QuickShare

# 2. Create virtual environment
python3 -m venv venv

# 3. Activate virtual environment
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

---

## 6. How to Start the Application

Start the QuickShare server:

```powershell
python Quickshare.py
```

### Server Output:
```
2026-08-31 20:00:00 [INFO] Starting QuickShare server...
 * Serving Flask app 'Quickshare'
 * Debug mode: on
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.1.100:5000
```

### Accessing the Web Interface:
1. **On the host PC**: Open `http://localhost:5000` or `http://127.0.0.1:5000` in your web browser.
2. **From other devices on the same Wi-Fi / LAN** (Phones, Tablets, Laptops):
   - Find your host PC's local IP address:
     - **Windows**: Run `ipconfig` (look for `IPv4 Address`, e.g., `192.168.1.100`).
     - **Linux / macOS**: Run `hostname -I` or `ifconfig`.
   - Open `http://<YOUR-LOCAL-IP>:5000` (e.g., `http://192.168.1.100:5000`) in the mobile browser.

---

## 7. Configuration

QuickShare supports configuration via environment variables:

| Environment Variable | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_CHUNK_SIZE` | `5242880` | Bytes | Chunk size for file uploads (Default: 5 MB). |
| `UPLOAD_CACHE_TIMEOUT` | `21600` | Seconds | Inactivity duration before an abandoned cache is purged (Default: 6 hours). |
| `CLEANUP_INTERVAL` | `1800` | Seconds | Frequency at which the cleanup worker scans for expired caches (Default: 30 minutes). |

### Example Configuration:

**PowerShell (Windows)**:
```powershell
$env:DEFAULT_CHUNK_SIZE="10485760"      # 10 MB chunks
$env:UPLOAD_CACHE_TIMEOUT="86400"        # 24 hours
python Quickshare.py
```

**Bash / Zsh (Linux / macOS)**:
```bash
export DEFAULT_CHUNK_SIZE=10485760
export UPLOAD_CACHE_TIMEOUT=86400
python3 Quickshare.py
```

---

## 8. REST API Endpoints

### 1. Start Upload Session
- **URL**: `POST /upload/start`
- **Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "filename": "archive.zip",
    "total_size": 104857600,
    "chunk_size": 5242880,
    "file_hash": "optional_sha256_hash"
  }
  ```
- **Response (201 Created)**:
  ```json
  {
    "success": true,
    "upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84",
    "chunk_size": 5242880,
    "total_chunks": 20,
    "status": "uploading"
  }
  ```

### 2. Upload Single Chunk
- **URL**: `POST /upload/chunk`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `upload_id`: UUID string
  - `chunk_index`: Integer (0-indexed)
  - `total_chunks`: Integer
  - `chunk`: Raw binary chunk file
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84",
    "chunk_index": 0,
    "received_count": 1
  }
  ```
- **Error Codes**: `400` (Bad chunk bounds or size mismatch), `404` (Upload not found), `409` (Assembly already in progress).

### 3. Query Upload Status (Resume)
- **URL**: `GET /upload/status/<upload_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84",
    "filename": "archive.zip",
    "safe_filename": "archive.zip",
    "total_size": 104857600,
    "chunk_size": 5242880,
    "total_chunks": 20,
    "received_chunks": [0, 1, 2],
    "missing_chunks": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
    "next_chunk": 3,
    "status": "uploading"
  }
  ```

### 4. Complete & Verify Upload
- **URL**: `POST /upload/complete`
- **Headers**: `Content-Type: application/json`
- **Request Body**: `{"upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84"}`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "filename": "archive.zip",
    "size": 104857600,
    "sha256": "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
    "message": "archive.zip uploaded and verified successfully"
  }
  ```

### 5. Cancel Upload
- **URL**: `POST /upload/cancel/<upload_id>` or `DELETE /upload/<upload_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "message": "Upload cancelled and cache purged successfully"
  }
  ```

### 6. Download File
- **URL**: `GET /download/<filename>`
- **Response**: Binary file stream with `Content-Disposition: attachment`.

---

## 9. Upload State Machine

| Current State | Event | Next State | Description |
|---|---|---|---|
| `initializing` | `/upload/start` succeeds | `uploading` | Cache directory and metadata initialized. |
| `uploading` | Chunk received | `uploading` | Staging chunks inside `cache/<upload_id>/chunks/`. |
| `uploading` | User clicks Pause | `paused` | In-flight requests aborted; cache preserved. |
| `paused` | User clicks Resume | `uploading` | Queries authoritative state and resumes missing chunks. |
| `uploading` / `paused` | User clicks Cancel | `cancelled` | In-flight requests aborted; cache purged immediately. |
| `uploading` | All chunks received & `/upload/complete` called | `assembling` | **No new chunks accepted (HTTP 409)**. |
| `assembling` | Streaming assembly & hash validation succeeds | `completed` | Atomically moved to `uploads/`; cache purged. |
| `assembling` | Size or hash mismatch occurs | `failed` | Assembled temp file removed; error logged. |
| `assembling` | Cancellation occurs during assembly | `cancelled` | Cancel wins race; temp file deleted. Final file never created. |

---

## 10. Cache Management & Structure

Each upload session resides inside an isolated directory under `cache/`:

```
cache/
└── 4b684534-1299-4c5b-801b-5e92c2df6d84/
    ├── metadata.json
    ├── chunks/
    │   ├── 000000
    │   ├── 000001
    │   └── ...
    └── assembled.tmp (present only during assembly)
```

- **Cancellation**: Entire `cache/<upload_id>/` directory deleted immediately.
- **Success / Finalization**: Moved atomically to `uploads/<filename>`, cache directory purged.
- **Pause / Network Failure**: Staging cache preserved intact.
- **Abandoned Uploads**: Cleaned automatically when inactivity exceeds `UPLOAD_CACHE_TIMEOUT`.
- **Assembling Protection**: Uploads marked `status == "assembling"` are strictly exempt from automated cache expiration.

---

## 11. Security Model

- **UUID Validation**: All upload identifiers are strictly validated against UUID regex.
- **Path Traversal Protection**: Uses `os.path.commonpath` to verify that all operations stay strictly within `uploads/` or `cache/`.
- **Filename Sanitization**: Sanitizes names with `secure_filename`, preventing path escape or command injection while preserving extensions.
- **Download Isolation**: Serves only verified completed files residing directly in `uploads/`. Attempting to access cache files, dotfiles, or parent directories returns `403 Forbidden` or `404 Not Found`.

> [!NOTE]
> QuickShare is designed for trusted local networks (home/office LAN). It does not include built-in user authentication, encryption at rest, antivirus scanning, or rate limiting. For exposure over public networks, place QuickShare behind a reverse proxy (e.g., Nginx, Caddy) with HTTPS and authentication.

---

## 12. Automated Test Suite

QuickShare includes a 28-scenario automated integration test suite:

```powershell
python test_quickshare.py
```

### Verified Automated Scenarios:
1. `test_01_small_file_upload`: Small file single-chunk transfer.
2. `test_02_large_file_upload`: Multi-chunk 5MB transfer and SHA-256 verification.
3. `test_03_zero_byte_file`: 0-byte file upload and empty hash verification.
4. `test_04_chunk_rejected_during_assembly`: Verifies HTTP 409 rejection during assembly.
5. `test_05_cancel_at_10_percent`: Early cancellation and cache purge.
6. `test_06_cancel_at_50_percent`: Mid-transfer cancellation.
7. `test_07_cancel_near_completion`: 90% cancellation verification.
8. `test_08_network_interruption`: Verifies cache retention during connection drop.
9. `test_09_resume_after_interruption`: Reconnection and missing-chunk-only transfer.
10. `test_10_duplicate_chunk`: Repeated upload of the same chunk (idempotency).
11. `test_11_genuine_concurrent_uploads`: Multi-threaded parallel file uploads via `ThreadPoolExecutor`.
12. `test_12_filename_collision_handling`: Safe deduplication (`duplicate_name (1).txt`).
13. `test_13_assembling_upload_protected_from_cleanup`: Assembling uploads exempt from cache purge.
14. `test_14_server_restart_cache_persistence`: Chunk state persisted across server restarts.
15. `test_15_expired_abandoned_cache_cleanup`: Expired cache cleanup verification.
16. `test_16_completed_file_download`: Verified download from `uploads/`.
17. `test_17_incomplete_files_not_downloadable`: Blocks downloads of incomplete/cache files.
18. `test_18_security_and_path_traversal`: Blocks `../`, fake UUIDs, and traversal attacks.
19. `test_19_streaming_assembly_memory_efficiency`: Fixed 64KB buffer multi-chunk streaming.
20. `test_20_repeated_completion_request`: Idempotent completion call handling.
21. `test_21_repeated_cancellation`: Idempotent cancellation call handling.
22. `test_22_invalid_upload_id`: 400/404 handling on invalid UUIDs.
23. `test_23_invalid_chunk_index`: Bounds validation (negative/oversized chunk indexes).
24. `test_24_invalid_total_chunks`: Mismatch rejection on chunk count.
25. `test_25_cancel_complete_race`: Multi-threaded cancel vs complete race resolution.
26. `test_26_metadata_corruption_handling`: Safe recovery on invalid JSON metadata.
27. `test_27_edge_file_sizes`: 1-byte, 1 KB, exact 1 chunk, 1 chunk + 1 byte, exact multiple.
28. `test_28_active_and_paused_cache_preserved_during_cleanup`: Verifies active/paused uploads are not expired prematurely.

---

## 13. Manual Browser & Device Testing

While the backend and concurrency logic are verified via automated unit tests, the following client behaviors should be verified manually across real devices:

### Recommended Manual Checklist:
- **Browsers to Test**: Google Chrome, Microsoft Edge, Mozilla Firefox, Apple Safari (iOS), Android Chrome.
- **Tab Switching**: Start an upload, switch to another browser tab for 2 minutes, return to the tab, and confirm transfer reconciles and completes.
- **Network Toggle**: Start an upload, disable Wi-Fi on the client device for 10 seconds, re-enable Wi-Fi, and verify automatic recovery without restarting from chunk 0.
- **Manual Pause**: Click Pause, switch tabs or disconnect network, return and confirm status remains **PAUSED** until Resume is explicitly clicked.
- **Page Refresh**: Refresh browser mid-upload, confirm the interrupted upload banner appears, select the matching file, and verify seamless resume.
- **Mobile Screen Sizing**: Test on mobile viewports (320px, 375px, 430px) to verify touch targets (≥44px) and zero horizontal scroll.

---

## 14. Known Limitations & Realities

- **Browser File Handle Access Across Reloads**: Modern web browsers do not permit JavaScript to retain direct file system handles across hard page reloads for security reasons. When reloading during an upload, QuickShare displays the interrupted session notification, prompting the user to re-select the matching file to instantly resume from the exact missing chunk.
- **Operating System Background Throttling**: When a browser tab is minimized, backgrounded, or a mobile phone screen is locked, operating systems throttle JavaScript execution and background network bandwidth. QuickShare adapts gracefully using promise queues and exponential backoff, resuming automatically when the tab becomes active.

---

## 15. Production Deployment Notes

For production environments, run QuickShare behind a production WSGI server such as **Waitress** (Windows) or **Gunicorn** (Linux/macOS) with **Nginx** as a reverse proxy:

```bash
# Linux / macOS with Gunicorn
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 Quickshare:app

# Windows with Waitress
pip install waitress
waitress-serve --port=5000 Quickshare:app
```

---

## 16. Quick Start

1. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
2. **Start the server**:
   ```powershell
   python Quickshare.py
   ```
3. **Open browser**:
   Navigate to `http://localhost:5000` to start sharing files!
