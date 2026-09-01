# QuickShare

**QuickShare** is a lightweight, reliable, chunked, and resumable Local Area Network (LAN) file-sharing application. It utilizes a Python Flask coordination server to facilitate fast, secure, and resilient transfers between devices (e.g., PC to mobile phone, laptop to desktop) connected to the same Wi-Fi or Ethernet network.

---

## 1. Project Overview & LAN Architecture

QuickShare is designed specifically for trusted local network transfers. It does **not** rely on third-party cloud servers, external databases, or complex WebRTC signaling. The Flask server serves as the central coordination and transfer hub.

```text
[ Device A (e.g., Laptop / PC) ]
               |
               v (HTTP Chunked Uploads over LAN)
[ QuickShare Flask Server ] ---> Staging Cache (cache/<upload_id>/) ---> Verified Storage (uploads/)
               |
               v (HTTP Direct Download over LAN with Range Support)
[ Device B (e.g., Phone / Tablet) ]
```

---

## 2. Key Features
- **LAN Optimized**: Dynamically discovers and displays the primary LAN IP and generates a scannable QR code for instant phone-to-PC connections via the **Connect device** modal.
- **Folder Upload & Sharing**: Upload entire directory trees via the folder picker or drag-and-drop. Internal directory structures are strictly preserved on the server under `uploads/<folder_name>/`.
- **Interactive Folder Explorer**: In-browser folder navigation modal with dynamic breadcrumb navigation (`Home / MyProject / src`), clickable subfolders, search within folders, individual file downloads, and one-click **Download ZIP**.
- **Streaming Folder ZIP Downloads**: Download any shared folder as a single ZIP archive on-the-fly (`GET /download/zip/<folder_name>`). Uses disk-backed streaming with automatic cleanup, preventing RAM exhaustion.
- **Chunked Transfers**: Slices large files into manageable 8 MB chunks via browser `File.slice()`, avoiding request timeouts and payload limits.
- **Resumable Transfers**: Interrupted transfers resume from the exact missing chunk without re-uploading completed chunks.
- **True Pause & Resume**: Manual pause immediately aborts in-flight network requests (`AbortController`) while safely preserving the server staging cache. Background events (`online`, `visibilitychange`) will never override an explicit manual pause.
- **Instant Cancellation**: Users can cancel an upload at any time; active network requests are aborted and the staging cache is deleted immediately.
- **SHA-256 File Integrity Verification**: Computes the SHA-256 hash incrementally during assembly and verifies byte count against the client metadata before publishing.
- **Controlled Concurrency**: 4 concurrent chunk workers per file (`UPLOAD_CONCURRENCY = 4`) for optimal network saturation on 5 GHz Wi-Fi and high-speed LANs.
- **Streaming Assembly**: Large files are assembled using a fixed 1 MB streaming buffer (`STREAM_BUFFER_SIZE = 1024 * 1024`), guaranteeing low memory consumption regardless of file size.
- **Safe Receiver Downloads**: Downloads support HTTP Range requests (`206 Partial Content`), media seeking, nested sub-paths, and multi-client concurrent streaming strictly from `uploads/`. Receiver cancellations or network drops never delete server files.
- **Collision-Safe Filenames & Folders**: Automatically generates unique names (e.g., `MyProject (1)`) without overwriting existing items in `uploads/`.
- **Cache Isolation**: In-progress chunks remain strictly inside `cache/` and are never accessible via download endpoints until verified and finalized.
- **Automatic Cache Cleanup**: Background worker daemon safely purges abandoned incomplete uploads older than `UPLOAD_CACHE_TIMEOUT` (default: 6 hours), while strictly protecting active assemblies (`status == "assembling"`).
- **Browser Session Recovery**: Active upload sessions are tracked in browser `localStorage`. When the page is reloaded, an interrupted upload prompt allows instant resume upon file selection.
- **Network & Tab Visibility Recovery**: Automatically reconciles missing chunk state with the server when connection is restored or when returning to a backgrounded tab.
- **Per-Upload Locking (`UploadLockManager`)**: Independent uploads synchronize on their own isolated lock with reference counting, eliminating global thread contention.
- **Multi-File & Folder Queue**: Supports selecting or dropping multiple files and directories simultaneously. Enqueues items with dedicated progress tracks, isolated controls, and controlled concurrency (`MAX_CONCURRENT_FILES = 2`, each with 4 chunk workers).
- **Professional Categorized UI**: Modern dark theme with file-type icons, instant search, horizontal mobile category filtering (All, Folders, Images, Videos, Audio, Documents, Archives, Code, Applications, Other), and zero emojis.

---

## 3. Directory Structure

```text
QuickShare/
|-- Quickshare.py          # Flask application, dynamic LAN discovery & embedded responsive UI
|-- requirements.txt       # Production dependencies (Flask, Werkzeug, qrcode)
|-- README.md              # Operational and technical manual
|-- .gitignore             # Excludes uploads/, cache/, venv/, and temporary runtime files
|-- uploads/               # Verified completed files and folders (auto-created on startup)
\-- cache/                 # Temporary chunk and folder staging directories (auto-created on startup)
```

---

## 4. Requirements
- **Python**: Python 3.8 or higher (Python 3.10+ recommended)
- **Dependencies**:
  - `Flask >= 3.0.0`
  - `Werkzeug >= 3.0.0`
  - `qrcode >= 7.4.2`
- **Supported Operating Systems**: Windows, Linux, macOS
- **Browser Requirements**: Modern browser supporting `fetch`, `AbortController`, `File.slice()`, `webkitdirectory`, and `localStorage` (Chrome, Edge, Firefox, Safari).

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
```text
2026-08-31 20:00:00 [INFO] Starting QuickShare LAN File Transfer Server on http://192.168.1.105:5000 (Listening on 0.0.0.0:5000, debug=False)
 * Serving Flask app 'Quickshare'
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.1.105:5000
```

### Accessing QuickShare from Other LAN Devices:
1. **On the host computer**: Open `http://localhost:5000` or `http://127.0.0.1:5000`.
2. **From phones, tablets, or other laptops on the same Wi-Fi / LAN**:
   - **QR Code**: Click **"Connect device"** in the top navigation bar and scan the QR code with your phone camera.
   - **Direct URL**: Navigate to `http://<LAN-IP>:5000` (e.g., `http://192.168.1.105:5000`).

---

## 7. Configuration

All server parameters can be customized via environment variables:

| Environment Variable | Default Value | Unit | Description |
|---|---|---|---|
| `HOST` | `0.0.0.0` | IP String | Network interface to bind to (`0.0.0.0` binds to all LAN interfaces). |
| `PORT` | `5000` | Integer | TCP port to listen on. |
| `DEBUG` | `false` | Boolean | Enables Flask debug mode (`true` or `false`). Default is `false` for normal LAN sharing. |
| `DEFAULT_CHUNK_SIZE` | `8388608` | Bytes | Chunk size for file uploads (Default: 8 MB). |
| `UPLOAD_CACHE_TIMEOUT` | `21600` | Seconds | Inactivity duration before an abandoned cache is purged (Default: 6 hours). |
| `CLEANUP_INTERVAL` | `1800` | Seconds | Frequency at which the cleanup worker scans for expired caches (Default: 30 minutes). |

---

## 8. REST API Endpoints

### 1. Start Folder Upload Session
- **URL**: `POST /folder/upload/start`
- **Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "folder_name": "MyProject",
    "total_files": 3,
    "total_size": 52428800,
    "files": [
      { "relative_path": "README.md", "size": 1024 },
      { "relative_path": "src/main.py", "size": 2048 },
      { "relative_path": "assets/logo.png", "size": 52425728 }
    ]
  }
  ```
- **Response (201 Created)**:
  ```json
  {
    "success": true,
    "folder_id": "9000dd3a-9295-49a3-9c96-9ccbccde9f5d",
    "folder_name": "MyProject",
    "total_files": 3,
    "total_size": 52428800
  }
  ```

### 2. Start File Upload Session (Standalone or Folder Sub-File)
- **URL**: `POST /upload/start`
- **Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "filename": "main.py",
    "total_size": 2048,
    "chunk_size": 8388608,
    "folder_id": "9000dd3a-9295-49a3-9c96-9ccbccde9f5d",
    "relative_path": "src/main.py"
  }
  ```
- **Response (201 Created)**:
  ```json
  {
    "success": true,
    "upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84",
    "chunk_size": 8388608,
    "total_chunks": 1,
    "status": "uploading"
  }
  ```

### 3. Upload Single Chunk
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

### 4. Complete File Upload
- **URL**: `POST /upload/complete`
- **Headers**: `Content-Type: application/json`
- **Request Body**: `{"upload_id": "4b684534-1299-4c5b-801b-5e92c2df6d84"}`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "filename": "main.py",
    "size": 2048,
    "sha256": "d79a8bbd00970f3dd5fa77381a74cd0dfcaa8f4e6f3db97ed3dc2be2233fc147"
  }
  ```

### 5. Finalize & Publish Folder
- **URL**: `POST /folder/upload/complete`
- **Headers**: `Content-Type: application/json`
- **Request Body**: `{"folder_id": "9000dd3a-9295-49a3-9c96-9ccbccde9f5d"}`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "folder_name": "MyProject",
    "total_files": 3,
    "total_size": 52428800,
    "message": "Folder MyProject uploaded and published successfully"
  }
  ```

### 6. Cancel Folder Upload
- **URL**: `POST /folder/upload/cancel/<folder_id>` or `DELETE /folder/upload/cancel/<folder_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "message": "Folder upload cancelled and cache purged"
  }
  ```

### 7. Explore Folder Contents
- **URL**: `GET /folder/contents/<path:folder_path>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "current_path": "MyProject",
    "breadcrumbs": [
      { "name": "MyProject", "path": "MyProject" }
    ],
    "items": [
      { "name": "src", "relative_path": "MyProject/src", "is_folder": true, "file_count": 1, "size": 2048, "size_str": "2.0 KB" },
      { "name": "README.md", "relative_path": "MyProject/README.md", "is_folder": false, "size": 1024, "size_str": "1.0 KB" }
    ]
  }
  ```

### 8. Download Folder as ZIP
- **URL**: `GET /download/zip/<path:folder_name>`
- **Response**: Streaming ZIP archive (`application/zip`) containing the full directory structure with `Content-Disposition: attachment; filename="MyProject.zip"`.

### 9. Download File (Root or Nested)
- **URL**: `GET /download/<path:filepath>`
- **Response**: Binary file stream supporting HTTP Range requests (`206 Partial Content`) and `Content-Disposition: attachment`.

### 10. LAN QR Code
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

```text
cache/
\-- 4b684534-1299-4c5b-801b-5e92c2df6d84/
    |-- metadata.json
    |-- chunks/
    |   |-- 000000
    |   |-- 000001
    |   \-- ...
    \-- assembled.tmp (present only during streaming assembly)
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

- **QR Connection**: Scan QR code from phone camera via "Connect device", verify homepage loads instantly over Wi-Fi.
- **Phone to PC Transfer**: Select a photo/video on phone, verify fast chunked upload to PC.
- **PC to Phone Download**: Download uploaded files on mobile and verify file integrity.
- **Tab Switching**: Switch tabs mid-upload, wait 1 minute, return, verify seamless resume.
- **Wi-Fi Drop / Reconnect**: Toggle Wi-Fi on phone for 5 seconds; verify upload continues from missing chunks without restarting.
- **Manual Pause**: Pause upload, verify background events do not restart transfer until Resume is clicked.
- **Page Refresh**: Refresh browser mid-upload, verify interrupted session banner prompts file re-selection to resume.

---

## 13. Production Deployment Notes

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

## 14. Quick Start

1. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
2. **Start the server**:
   ```powershell
   python Quickshare.py
   ```
3. **Open browser**:
   Navigate to `http://localhost:5000` or scan the QR code from your mobile device.
