# QuickShare

**QuickShare** is a fast, resilient, chunked, and resumable local file-sharing server built with Python and Flask. It is designed to handle files of any size reliably over unstable network connections, surviving page reloads, browser background throttling, and network dropouts while preventing partial file corruption.

---

## 1. Project Title
**QuickShare — Fast, Resumable Local File Sharing**

QuickShare enables peer-to-peer file transfers across local networks with an isolated temporary caching architecture, atomic file finalization, and server-authoritative resume capabilities.

---

## 2. Features
- **Chunked File Uploads**: Large files are split into manageable chunks (default 5 MB) to avoid huge request payloads and high RAM consumption.
- **Resumable Transfers**: Interrupted transfers resume from the exact missing chunk without restarting from byte zero.
- **Pause & Resume Controls**: Users can pause active uploads, which aborts active chunk requests while safely retaining temporary state on the server.
- **Instant Cancellation**: Users can cancel an upload at any time; in-flight network requests are aborted via `AbortController` and server-side cache is deleted immediately.
- **SHA-256 File Integrity Verification**: Ensures the final assembled file matches the client's original file hash and exact byte size.
- **Controlled Client Concurrency**: Multi-worker chunk dispatcher uploads multiple chunks concurrently (default 3 concurrent workers per file).
- **Streaming Assembly**: Large files are assembled using a 64 KB streaming buffer, ensuring minimal memory footprint for multi-gigabyte files.
- **Collision-Safe Filenames**: Automatically generates unique names (e.g., `movie (1).mp4`) without overwriting existing files in `uploads/`.
- **Cache Isolation**: In-progress chunks are kept strictly inside `cache/<upload_id>/` and never appear in the downloadable files list until fully verified.
- **Automatic Cache Expiration & Cleanup**: Background worker daemon purges abandoned incomplete uploads older than the configured timeout (`UPLOAD_CACHE_TIMEOUT`).
- **Browser Session Recovery**: Active upload sessions are tracked in browser `localStorage`. When the page is reloaded, an interrupted upload prompt allows instant resume upon file selection.
- **Network & Tab Visibility Recovery**: Gracefully handles network loss and browser tab background throttling via promise queues and generation tokens.
- **Per-Upload Locking**: Independent uploads synchronize on their own fine-grained lock, preventing global thread contention.
- **Atomic Operations**: Metadata files and assembled files use atomic file replacement (`os.replace`) to prevent file corruption during sudden server stops.
- **Secure Downloads**: Downloads are served strictly from `uploads/` with path-containment validation.

---

## 3. Architecture Overview

QuickShare strictly separates in-progress transfers from verified, downloadable files:

```
[ Browser / Client ]
       │
       ▼ (POST /upload/start)
[ Session Initialization ] ──► Creates cache/<upload_id>/metadata.json
       │
       ▼ (POST /upload/chunk)
[ Chunk Staging ] ───────────► Saves chunks into cache/<upload_id>/chunks/000000...
       │
       ▼ (POST /upload/complete)
[ 3-Phase Assembly ] ────────► Streams chunks into cache/<upload_id>/assembled.tmp
       │                       (Verifies byte size and SHA-256 hash)
       │
       ▼ (Atomic Move)
[ Final Publication ] ───────► Moves assembled.tmp atomically to uploads/<filename>
       │                       (Purges cache/<upload_id>/)
       │
       ▼ (GET /download/<filename>)
[ Secure Downloads ] ────────► Serves verified files strictly from uploads/
```

### Directory Roles
- **`uploads/`**: Contains **only** complete, verified, downloadable files.
- **`cache/`**: Contains temporary subdirectories for each upload session (`cache/<upload_id>/`).

---

## 4. Directory Structure

```
QuickShare/
├── Quickshare.py          # Main Flask application and embedded responsive frontend
├── requirements.txt       # Python dependencies
├── test_quickshare.py     # Comprehensive 26-scenario test suite
├── README.md              # Documentation and operational manual
├── .gitignore             # Excludes uploads/, cache/, and bytecode
├── uploads/               # Completed files (auto-created on startup)
└── cache/                 # Temporary in-progress chunks (auto-created on startup)
```

---

## 5. Requirements
- **Python**: Python 3.8 or higher (Python 3.10+ recommended)
- **Dependencies**:
  - `Flask >= 3.0.0`
  - `Werkzeug >= 3.0.0`
- **Supported Operating Systems**: Windows, Linux, macOS
- **Browser Requirements**: Modern browser supporting `fetch`, `AbortController`, `File.slice()`, and `localStorage` (Chrome, Edge, Firefox, Safari).

---

## 6. Installation

### Windows (PowerShell)
```powershell
# Clone or navigate to the repository
cd C:\path\to\QuickShare

# Create virtual environment
python -m venv venv

# Activate virtual environment
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Linux / macOS (Bash / Zsh)
```bash
# Clone or navigate to the repository
cd /path/to/QuickShare

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 7. How to Start the Application

To run the QuickShare server:

```powershell
python Quickshare.py
```

### Server Output:
```
2026-08-31 16:30:00 [INFO] Starting QuickShare server...
 * Serving Flask app 'Quickshare'
 * Debug mode: on
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://<your-local-ip>:5000
```

### Accessing the Web Interface:
- **On the host machine**: Open `http://localhost:5000` or `http://127.0.0.1:5000` in your web browser.
- **From another device on the same local Wi-Fi / LAN**: Open `http://<your-local-ip>:5000` (e.g., `http://192.168.1.100:5000`).

---

## 8. Configuration

QuickShare supports configuration via environment variables:

| Environment Variable | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_CHUNK_SIZE` | `5242880` | Bytes | Chunk size for file transfers (Default: 5 MB). |
| `UPLOAD_CACHE_TIMEOUT` | `21600` | Seconds | Inactivity duration before an abandoned cache is expired and purged (Default: 6 hours). |
| `CLEANUP_INTERVAL` | `1800` | Seconds | Frequency at which the background cleanup worker scans for expired caches (Default: 30 minutes). |

### Setting Environment Variables:

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

## 9. Upload Lifecycle

1. **Start (`POST /upload/start`)**: Client sends filename and total size. Server returns a unique UUID `upload_id`.
2. **Chunk Transmission (`POST /upload/chunk`)**: Frontend reads slices of the file using `File.slice()` and uploads them concurrently. Server stores chunks as `cache/<upload_id>/chunks/<index:06d>`.
3. **Assembly Initiation (`POST /upload/complete`)**: Server verifies all chunks exist and transitions status to `assembling`.
4. **Streaming Assembly (Phase 2)**: Server streams chunks in order into `cache/<upload_id>/assembled.tmp` via 64 KB buffers, calculating the SHA-256 hash and accumulated byte size.
5. **Finalization & Atomic Move (Phase 3)**: Server re-verifies upload state (ensuring cancellation did not occur during assembly), generates a non-colliding destination filename in `uploads/`, moves `assembled.tmp` atomically, and purges the session cache.

---

## 10. Resume System

- **Authoritative Server State**: The client queries `GET /upload/status/<upload_id>` to fetch the authoritative list of received chunks from disk.
- **Missing Chunks Computation**: The client calculates `missing_chunks = total_chunks - received_chunks` and uploads only those chunks.
- **Fingerprint Identity Check**: When resuming an interrupted session from `localStorage`, the frontend validates `filename`, `total_size`, and `lastModified` timestamp to ensure the user does not resume with an mismatched file.

---

## 11. Pause and Resume

- **Pause**: Setting state to `paused` aborts all active `fetch` chunk requests immediately using `AbortController`. The server cache and metadata are retained intact.
- **Resume**: Increments the internal `uploadGeneration` token to invalidate old worker threads, queries `/upload/status/<upload_id>` for the latest missing chunks list, and restarts workers on the missing chunks only.

---

## 12. Cancellation

- Pressing **Cancel** immediately aborts in-flight fetch requests, stops queued workers from picking up new chunks, sends `POST /upload/cancel/<upload_id>` to the server, and purges the `cache/<upload_id>/` directory.
- **Race with Assembly**: If cancellation arrives while the server is assembling, Phase 3 detects that `status == "cancelled"`, discards `assembled.tmp`, and deletes the cache without creating any file in `uploads/`.

---

## 13. Network Recovery & Background Tab Behavior

- **Network Offline/Online**: When network connectivity drops, the client performs exponential backoff retries. When the browser fires the `online` event, QuickShare re-queries `/upload/status` and resumes missing chunks.
- **Tab Visibility Changes**: When a tab is backgrounded, browsers throttle timer and network execution. QuickShare's worker queue adapts to throttled throughput without crashing. When the tab becomes visible (`visibilitychange`), QuickShare checks active uploaders and revives stalled workers.

---

## 14. Chunk Upload REST API

### 1. Start Upload Session
- **URL**: `POST /upload/start`
- **Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "filename": "movie.mp4",
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
    "filename": "movie.mp4",
    "safe_filename": "movie.mp4",
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
    "filename": "movie.mp4",
    "size": 104857600,
    "sha256": "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
    "message": "movie.mp4 uploaded and verified successfully"
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

## 15. Upload State Machine

| Current State | Event | Next State | Description |
|---|---|---|---|
| `initializing` | `/upload/start` succeeds | `uploading` | Cache directory and metadata initialized. |
| `uploading` | Chunks received | `uploading` | Staging chunks inside cache. |
| `uploading` | User clicks Pause | `paused` | In-flight requests aborted; cache preserved. |
| `paused` | User clicks Resume | `uploading` | Reconciles missing chunks and continues. |
| `uploading` / `paused` | User clicks Cancel | `cancelled` | In-flight requests aborted; cache purged. |
| `uploading` | All chunks received & `/upload/complete` called | `assembling` | **No new chunks accepted (HTTP 409)**. |
| `assembling` | Streaming assembly & hash validation succeeds | `completed` | Atomically moved to `uploads/`; cache purged. |
| `assembling` | Size or hash mismatch occurs | `failed` | Assembled temp file removed; error logged. |
| `assembling` | Cancellation occurs during assembly | `cancelled` | Assembly discarded; cache purged. Final file never created. |

---

## 16. File Integrity Verification

- **Authoritative Size Check**: The server checks that `assembled_size == expected_total_size`.
- **SHA-256 Stream Verification**: If the client provides a SHA-256 hash at upload start, the server computes the hash while streaming chunks into `assembled.tmp` and validates the digest. If a mismatch is detected, the assembly fails and the temporary file is deleted.
- **Zero-Byte File Support**: An empty file (0 bytes) is handled with `total_chunks = 1`, an expected chunk size of 0 bytes, and an empty file hash `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

---

## 17. Security Specifications

- **UUID Validation**: All upload identifiers are checked against strict UUID regex (`^[a-f0-9\-]{36}$`).
- **Path Traversal Protection**: Uses `os.path.commonpath` to verify that all file operations remain strictly inside `uploads/` or `cache/`.
- **Filename Sanitization**: Utilizes `werkzeug.utils.secure_filename` to prevent special characters, command injections, and path traversal in file names.
- **Isolated Downloads**: Only regular files residing strictly inside `uploads/` can be downloaded. Cache directories, chunk files, dotfiles, and system paths return `403 Forbidden` or `404 Not Found`.

> [!NOTE]
> QuickShare is designed for trusted local networks (LAN / Wi-Fi). It does not provide built-in user authentication, role-based access control, or HTTPS certificates by default.

---

## 18. Concurrency Model

- **Per-Upload Locking (`UploadLockManager`)**: Each `upload_id` has an independent `threading.Lock()` managed with reference counting. Multiple uploads do not block or serialize each other.
- **Non-Blocking Assembly**: Lock is released during Phase 2 (streaming I/O and hashing) so other uploads continue uninterrupted.
- **Filename Collision Lock**: A lightweight global mutex is acquired only momentarily when determining non-colliding destination filenames (`movie (1).mp4`).

---

## 19. Cache Management

- **Structure**: Each active upload resides in `cache/<upload_id>/`.
- **Protection During Assembly**: Uploads marked as `status == "assembling"` are strictly exempt from automated cache expiration.
- **Cleanup Worker**: Periodically checks for inactive sessions older than `UPLOAD_CACHE_TIMEOUT` and deletes them safely.

---

## 20. Large File Handling

- **Memory Efficiency**: Chunks are assembled using a fixed 64 KB buffer (`infile.read(64 * 1024)`). Memory consumption remains constant (~a few megabytes) whether uploading a 1 MB photo or a 50 GB ISO.

---

## 21. Troubleshooting Guide

| Problem | Cause | Solution |
|---|---|---|
| `Address already in use (port 5000)` | Another process is using port 5000. | Stop the existing process or run on a different port: `python Quickshare.py` with modified port. |
| `Upload failed: Chunk size mismatch` | Network payload was truncated. | The client will automatically retry the chunk with exponential backoff. |
| `Upload session expired or not found` | The upload cache was purged or server restarted past timeout. | Click **Dismiss** and select the file again to start a new upload. |
| `Download returns 403 Forbidden` | Attempted path traversal or access to non-upload directory. | Ensure the requested file exists in `uploads/`. |
| `Cannot connect from mobile device` | Firewall blocking port 5000 or different Wi-Fi network. | Allow port 5000 in Windows Defender Firewall and ensure both devices are on the same Wi-Fi. |

---

## 22. Testing Guide

Run the full automated test suite containing 26 unit and integration test scenarios:

```powershell
python test_quickshare.py
```

### Verified Test Cases:
1. Small file upload
2. Multi-chunk large file (5MB)
3. Zero-byte empty file upload
4. Chunk upload rejected during assembly (HTTP 409)
5. Cancellation at 10%
6. Cancellation at 50%
7. Cancellation near completion (90%)
8. Network interruption & cache preservation
9. Resume after network interruption (missing chunks only)
10. Duplicate chunk idempotency
11. Multiple simultaneous uploads (3 parallel files)
12. Duplicate filename collision handling (`file (1).txt`)
13. Cache cleanup protection for assembling uploads
14. Server restart recovery
15. Expired abandoned cache cleanup
16. Completed file download
17. Incomplete files blocked from download
18. Path traversal and security protection
19. Streaming assembly memory efficiency
20. Repeated completion idempotency
21. Repeated cancellation idempotency
22. Invalid upload ID rejection (400 / 404)
23. Invalid chunk index bounds rejection
24. Invalid total_chunks rejection
25. Cancel vs Assembly race handling
26. Corrupted metadata recovery

---

## 23. API Testing Examples

### Starting an Upload using PowerShell:
```powershell
$body = @{
    filename = "sample.txt"
    total_size = 12
    chunk_size = 1048576
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri "http://localhost:5000/upload/start" -Method Post -Body $body -ContentType "application/json"
$uploadId = $response.upload_id
Write-Host "Created upload session: $uploadId"
```

### Querying Upload Status:
```powershell
Invoke-RestMethod -Uri "http://localhost:5000/upload/status/$uploadId" -Method Get
```

---

## 24. Development Notes
- Self-contained single-file architecture (`Quickshare.py`) makes QuickShare trivial to deploy without complex asset build pipelines.
- Modern CSS variables and flexbox/grid layout ensure full responsiveness across desktops, laptops, tablets, and phones.

---

## 25. Known Limitations
- **Browser File Handle Persistence**: Browsers do not permit JavaScript to retain direct file system handles across hard page reloads without user interaction. When reloading, the user must re-select the matching file to continue.
- **Browser Background Throttling**: Mobile and desktop browsers throttle timers and network connections when a tab is backgrounded. QuickShare adapts gracefully to throttled throughput, but cannot bypass OS-level background execution restrictions.

---

## 26. Future Improvements (Roadmap)
- Password-protected upload and download links.
- TLS/HTTPS support out of the box with self-signed certificate generation.
- Expiring one-time download links.
- QR code generation on the web interface for fast mobile connections.

---

## 27. Production Deployment Notes
For production environments, run QuickShare behind a WSGI server such as **Gunicorn** or **Waitress** with **Nginx** as a reverse proxy:

```bash
# Example with Gunicorn (Linux/macOS)
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 Quickshare:app

# Example with Waitress (Windows)
pip install waitress
waitress-serve --port=5000 Quickshare:app
```

---

## 28. Quick Start Summary

1. **Clone and navigate to repository**:
   ```powershell
   git clone https://github.com/SurendiranBJ/QuickShare.git
   cd QuickShare
   ```
2. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
3. **Start the server**:
   ```powershell
   python Quickshare.py
   ```
4. **Open in browser**:
   Navigate to `http://localhost:5000` to start sharing files!
