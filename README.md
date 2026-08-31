# QuickShare

**QuickShare** is a lightweight, reliable, chunked, and resumable Local Area Network (LAN) file-sharing application. It utilizes a Python Flask coordination server to facilitate fast, secure, and resilient transfers between devices (e.g., PC to mobile phone, laptop to desktop) connected to the same Wi-Fi or Ethernet network.

---

## 1. Project Overview & LAN Architecture

QuickShare is designed specifically for trusted local network transfers. It does **not** rely on third-party cloud servers, external databases, or complex WebRTC signaling. The Flask server serves as the central coordination and transfer hub.

```
[ Device A (e.g., Laptop / PC) ]
               │
               ▼ (HTTP Chunked Uploads over LAN)
[ QuickShare Flask Server ] ──► Staging Cache (cache/<upload_id>/) ──► Verified Storage (uploads/)
               │
               ▼ (HTTP Direct Download over LAN)
[ Device B (e.g., Phone / Tablet) ]
```

---

## 2. Key Features
- **LAN Optimized**: Dynamically discovers and displays the primary LAN IP and generates a scannable mobile QR code for instant phone-to-PC connections.
- **Chunked Transfers**: Slices large files into manageable 5 MB chunks via browser `File.slice()`, avoiding request timeouts and large payload errors.
- **Resumable Transfers**: Interrupted transfers resume from the exact missing chunk without re-uploading completed chunks.
- **True Pause & Resume**: Manual pause immediately aborts in-flight network requests (`AbortController`) while safely preserving the server staging cache. Background events (`online`, `visibilitychange`) will **never** override an explicit manual pause.
- **Instant Cancellation**: Users can cancel an upload at any time; active network requests are aborted and the staging cache is deleted immediately.
- **SHA-256 File Integrity Verification**: Computes the SHA-256 hash incrementally during assembly and verifies byte count against the client metadata before publishing.
- **Controlled Concurrency**: 3 concurrent chunk workers per file (`UPLOAD_CONCURRENCY = 3`) for optimal network saturation without router congestion.
- **Streaming Assembly**: Large files are assembled using a fixed 64 KB streaming buffer (`infile.read(64 * 1024)`), guaranteeing low memory consumption regardless of file size.
- **Collision-Safe Filenames**: Automatically generates unique names (e.g., `video (1).mp4`) without overwriting existing files in `uploads/`.
- **Cache Isolation**: In-progress chunks remain strictly inside `cache/<upload_id>/` and are never accessible via download endpoints until verified and finalized.
- **Automatic Cache Cleanup**: Background worker daemon safely purges abandoned incomplete uploads older than `UPLOAD_CACHE_TIMEOUT` (default: 6 hours), while strictly protecting active assemblies (`status == "assembling"`).
- **Browser Session Recovery**: Active upload sessions are tracked in browser `localStorage`. When the page is reloaded, an interrupted upload prompt allows instant resume upon file selection.
- **Network & Tab Visibility Recovery**: Automatically reconciles missing chunk state with the server when connection is restored or when returning to a backgrounded tab.
- **Per-Upload Locking (`UploadLockManager`)**: Independent uploads synchronize on their own isolated lock with reference counting, eliminating global thread contention.
- **Atomic File Operations**: Metadata files and assembled files use atomic file replacement (`os.replace`) to prevent corruption during sudden server stops.
- **Mobile-First Responsive UI**: Centered card layout, fluid typography (`clamp`), touch-friendly 44px buttons, and zero horizontal scrolling.

---

## 3. Directory Structure

```
QuickShare/
├── Quickshare.py          # Flask application, dynamic LAN discovery & embedded responsive UI
├── requirements.txt       # Production dependencies (Flask, Werkzeug, qrcode)
├── README.md              # Complete operational and technical manual
├── .gitignore             # Excludes uploads/, cache/, venv/, and temporary runtime files
├── uploads/               # Verified completed files (auto-created on startup)
└── cache/                 # Temporary chunk staging directories (auto-created on startup)
```

---

## 4. Requirements
- **Python**: Python 3.8 or higher (Python 3.10+ recommended)
- **Dependencies**:
  - `Flask >= 3.0.0`
  - `Werkzeug >= 3.0.0`
  - `qrcode >= 7.4.2`
- **Supported Operating Systems**: Windows, Linux, macOS
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
2026-08-31 20:00:00 [INFO] Starting QuickShare LAN File Transfer Server on http://192.168.1.105:5000 (Listening on 0.0.0.0:5000, debug=False)
 * Serving Flask app 'Quickshare'
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.1.105:5000
```

### Accessing QuickShare from Other LAN Devices:
1. **On the host computer**: Open `http://localhost:5000` or `http://127.0.0.1:5000`.
2. **From phones, tablets, or other laptops on the same Wi-Fi / LAN**:
   - **QR Code**: Click **"📱 Mobile QR"** in the top banner on your PC screen and scan the QR code with your phone's camera.
   - **Direct URL**: Navigate to `http://<LAN-IP>:5000` (e.g., `http://192.168.1.105:5000`).

---

## 7. Configuration

All server parameters can be customized via environment variables:

| Environment Variable | Default Value | Unit | Description |
|---|---|---|---|
| `HOST` | `0.0.0.0` | IP String | Network interface to bind to (`0.0.0.0` binds to all LAN interfaces). |
| `PORT` | `5000` | Integer | TCP port to listen on. |
| `DEBUG` | `false` | Boolean | Enables Flask debug mode (`true` or `false`). Default is `false` for normal LAN sharing. |
| `DEFAULT_CHUNK_SIZE` | `5242880` | Bytes | Chunk size for file uploads (Default: 5 MB). |
| `UPLOAD_CACHE_TIMEOUT` | `21600` | Seconds | Inactivity duration before an abandoned cache is purged (Default: 6 hours). |
| `CLEANUP_INTERVAL` | `1800` | Seconds | Frequency at which the cleanup worker scans for expired caches (Default: 30 minutes). |

### Example Configuration:

**PowerShell (Windows)**:
```powershell
$env:PORT="8080"
$env:DEFAULT_CHUNK_SIZE="10485760"      # 10 MB chunks
python Quickshare.py
```

**Bash / Zsh (Linux / macOS)**:
```bash
export PORT=8080
export DEFAULT_CHUNK_SIZE=10485760
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

### 6. Download Completed File
- **URL**: `GET /download/<filename>`
- **Response**: Binary file stream with `Content-Disposition: attachment`.

### 7. LAN Mobile QR Code
- **URL**: `GET /qr`
- **Response**: SVG QR Code image (`image/svg+xml`) encoding the primary LAN URL.

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

## 10. Cache Structure & Management

Each upload session resides inside an isolated directory under `cache/`:

```
cache/
└── 4b684534-1299-4c5b-801b-5e92c2df6d84/
    ├── metadata.json
    ├── chunks/
    │   ├── 000000
    │   ├── 000001
    │   └── ...
    └── assembled.tmp (present only during streaming assembly)
```

- **Cancellation**: Entire `cache/<upload_id>/` directory deleted immediately.
- **Success / Finalization**: Moved atomically to `uploads/<filename>`, cache directory purged.
- **Pause / Network Failure**: Staging cache preserved intact.
- **Abandoned Uploads**: Cleaned automatically when inactivity exceeds `UPLOAD_CACHE_TIMEOUT`.
- **Assembling Protection**: Uploads marked `status == "assembling"` are strictly exempt from automated cache expiration.

---

## 11. Security Model

- **UUID Validation**: All upload identifiers are strictly validated against UUID regex.
- **Path Traversal Protection**: Uses `os.path.commonpath` to verify all operations stay strictly within `uploads/` or `cache/`.
- **Filename Sanitization**: Sanitizes names with `secure_filename`, preventing path escape or command injection while preserving extensions.
- **Download Isolation**: Serves only verified completed files residing directly in `uploads/`. Attempting to access cache files, dotfiles, or parent directories returns `403 Forbidden` or `404 Not Found`.

> [!NOTE]
> QuickShare is designed for trusted local networks (home/office LAN). It does not include built-in user authentication, encryption at rest, antivirus scanning, or rate limiting. For exposure over public networks, place QuickShare behind a reverse proxy (e.g., Nginx, Caddy) with HTTPS and authentication.

---

## 12. Manual Device Testing Checklist

The following client workflows can be verified across devices on your local network:

- **QR Connection**: Scan QR code from phone camera, verify homepage loads instantly over Wi-Fi.
- **Phone to PC Transfer**: Select a photo/video on phone, verify fast chunked upload to PC.
- **PC to Phone Download**: Download uploaded files on mobile and verify file integrity.
- **Tab Switching**: Switch tabs mid-upload, wait 1 minute, return, verify seamless resume.
- **Wi-Fi Drop / Reconnect**: Toggle Wi-Fi on phone for 5 seconds; verify upload continues from missing chunks without restarting.
- **Manual Pause**: Pause upload, verify background events do not restart transfer until Resume is clicked.
- **Page Refresh**: Refresh browser mid-upload, verify interrupted session banner prompts file re-selection to resume.

---

## 13. Known Limitations & Operating Realities

- **Browser File Handle Security**: Browsers do not permit JavaScript to retain direct file system handles across hard page reloads. When reloading during an upload, QuickShare displays the interrupted session notification, prompting the user to re-select the matching file to instantly resume from the exact missing chunk.
- **Operating System Background Throttling**: Mobile operating systems (iOS/Android) and desktop browsers throttle JavaScript timers and background network bandwidth when a tab is minimized or phone screen is locked. QuickShare adapts gracefully using promise queues and exponential backoff, resuming automatically when the tab becomes active.

---

## 14. Production Deployment Notes

For production environments, run QuickShare behind a WSGI server:

```bash
# Linux / macOS with Gunicorn
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 Quickshare:app

# Windows with Waitress
pip install waitress
waitress-serve --port=5000 Quickshare:app
```

---

## 15. Quick Start

1. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
2. **Start the server**:
   ```powershell
   python Quickshare.py
   ```
3. **Open browser**:
   Navigate to `http://localhost:5000` or scan the QR code from your mobile device!
