# QuickShare

QuickShare is a lightweight, reliable, chunked, and resumable Local Area Network (LAN) file-sharing application. It utilizes a Python Flask server to facilitate high-speed, direct file and folder transfers between devices (such as computers, smartphones, and tablets) connected to the same Wi-Fi or Ethernet network.

---

## 1. Project Introduction

QuickShare is engineered specifically for local network data exchange. Transfers take place directly between devices over your local network and do not pass through external cloud storage or third-party web services.

```text
+------------------------+
| Device A (Sender)      |
| Browser / Phone / PC   |
+------------------------+
            |
            | HTTP Chunked Uploads
            v
+------------------------+
| Local Network (LAN)    |
| Wi-Fi Router / Switch  |
+------------------------+
            |
            v
+------------------------+
| QuickShare Host Server | ---> Staging Cache (cache/) ---> Verified Storage (uploads/)
| Python / Flask App     |
+------------------------+
            |
            | HTTP Direct / Range / ZIP Downloads
            v
+------------------------+
| Device B (Receiver)    |
| Browser / Phone / PC   |
+------------------------+
```

### Key Network Characteristics
- **Local Network Transfers**: All data packets travel solely over your local Wi-Fi or Ethernet connection.
- **No Cloud Dependency**: Files are never sent to external servers, cloud databases, or third-party storage.
- **Direct Web Access**: Client devices interact through any modern web browser without requiring proprietary client-side software.
- **Local Reachability**: Devices must be connected to the same local network subnet or have an accessible route to the host IP.
- **Firewall & Isolation**: Client access depends on host firewall rules and local router settings (such as Access Point or Client Isolation).

---

## 2. Features

- **Single-File Upload**: Upload standalone files of any size using sliced chunk streaming.
- **Multi-File Selection & Queue**: Select or drag-and-drop multiple files simultaneously. Transfers are scheduled with controlled concurrency (`MAX_CONCURRENT_FILES = 2`).
- **Folder Upload & Sharing**: Upload complete directory trees via the folder picker or drag-and-drop. Internal folder hierarchies are preserved on disk under `uploads/<folder_name>/`.
- **Nested Folder Structure Preservation**: Subdirectories of arbitrary depth are created and populated accurately on the server.
- **Drag-and-Drop Support**: Drop files or entire folders directly onto the dropzone.
- **Chunked Slicing**: Slices large files into manageable 8 MB chunks (`DEFAULT_CHUNK_SIZE = 8388608`) via browser `Blob.slice()`, preventing HTTP timeouts and memory overload.
- **Resumable Transfers**: Interrupted transfers resume from the exact missing chunk without re-uploading completed data.
- **Pause & Resume Controls**: Pause uploads at any moment to abort active requests (`AbortController`) while preserving server-side chunks. Background events (`online`, `visibilitychange`) respect manual pause states.
- **Instant Cancellation**: Cancel transfers on demand to abort in-flight requests and immediately purge staging caches.
- **Automatic Retry with Backoff**: Chunks that encounter network hiccups retry up to 5 times (`MAX_CHUNK_RETRIES = 5`) with exponential backoff.
- **SHA-256 Integrity Verification**: Incremental SHA-256 hashing during file assembly ensures byte-level integrity before publishing to `uploads/`.
- **Isolated Upload Cache**: Incomplete transfers remain segregated inside `cache/` and are never accessible to download endpoints.
- **Automatic Cache Cleanup Worker**: A background thread periodically scans and purges abandoned staging caches older than `UPLOAD_CACHE_TIMEOUT` (default: 6 hours) while protecting active assemblies.
- **Controlled Worker Concurrency**: 4 concurrent chunk upload workers per active file (`UPLOAD_CONCURRENCY = 4`) maximize LAN throughput.
- **Per-Upload Thread Locking (`UploadLockManager`)**: Isolated mutexes with reference counting manage concurrent chunk writes per upload session without global thread contention.
- **Dynamic LAN IP Detection**: Automatically determines the primary host LAN IPv4 address and displays it on the interface and console.
- **Instant QR Device Connection**: Generates an offline SVG QR code allowing mobile devices to connect immediately by scanning the screen.
- **Filesystem-Driven Available Files**: Displays real, physical files and directories residing in `uploads/` with real-time updates and zero mock records.
- **Interactive Folder Explorer Modal**: Browse directory structures in the browser with interactive breadcrumbs, subfolder navigation, file-level downloads, and folder-level ZIP downloads.
- **Streaming Folder ZIP Downloads**: Generates and streams disk-backed ZIP archives on-the-fly (`GET /download/zip/<folder_name>`) with safe file handle cleanup.
- **HTTP Range Support (RFC 7233 / 9110)**: Supports `206 Partial Content` range requests for instant seeking in media files and resumable downloads.
- **Search & Filtering**: Search files and folders by name, or filter by file category (All, Folders, Images, Videos, Audio, Documents, Archives, Code, Applications, Other).
- **Categorized Vector Icons**: Visual file-type indicators with vector SVG graphics and no emojis.
- **Responsive Dark UI**: Clean interface built for desktops, laptops, tablets, and smartphones.
- **Collision-Safe Naming**: Automatically appends numeric indices (e.g., `Folder (1)` or `file (1).txt`) to avoid overwriting existing items.

---

## 3. How QuickShare Works

### File Upload Workflow
```text
Browser Client                           Flask Server                         Local Disk
      |                                        |                                  |
      |-- POST /upload/start ----------------->|                                  |
      |   (filename, total_size, chunk_size)   |-- Create cache/<upload_id>/ ---->|
      |<-- 201 Created (upload_id) ------------|                                  |
      |                                        |                                  |
      |-- POST /upload/chunk (Worker 1..4) --->|                                  |
      |   (upload_id, chunk_index, blob)       |-- Write chunk 000000 ----------->|
      |<-- 200 OK (received_count) ------------|                                  |
      |                                        |                                  |
      |-- POST /upload/complete -------------->|                                  |
      |   (upload_id)                          |-- Stream assemble chunks ------->|
      |                                        |-- Incremental SHA-256 check ---->|
      |                                        |-- Atomic move to uploads/ ------>|
      |                                        |-- Purge cache/<upload_id>/ ----->|
      |<-- 200 OK (filename, sha256) ----------|                                  |
```

### Folder Upload Workflow
```text
Browser Client                           Flask Server                         Local Disk
      |                                        |                                  |
      |-- POST /folder/upload/start ---------->|                                  |
      |   (folder_name, manifest)              |-- Create cache/<folder_id>/ ---->|
      |<-- 201 Created (folder_id) ------------|                                  |
      |                                        |                                  |
      | [For each file in folder manifest]     |                                  |
      |-- POST /upload/start (folder_id, rel)->|                                  |
      |-- POST /upload/chunk (chunks) -------->|-- Stage sub-file chunks -------->|
      |-- POST /upload/complete -------------->|-- Stage assembled sub-file ----->|
      |                                        |                                  |
      | [When all sub-files completed]         |                                  |
      |-- POST /folder/upload/complete ------->|                                  |
      |   (folder_id)                          |-- Move tree to uploads/<folder> >|
      |                                        |-- Purge cache/<folder_id>/ ----->|
      |<-- 200 OK (folder_name, published) ----|                                  |
```

### Download Workflow
- **Individual Files**: Download directly from `uploads/` via `GET /download/<filepath>`. Supports HTTP Range requests for video/audio streaming.
- **Folder Contents**: Inspect directories via `GET /folder/contents/<folder_path>` and download individual nested files.
- **Folder ZIP Archive**: Request `GET /download/zip/<folder_name>` to stream a dynamically generated ZIP archive of the entire folder.
- **Isolation Guarantee**: Incomplete uploads and temporary files remain inside `cache/` and cannot be accessed via download endpoints.

---

## 4. Requirements

### Python Environment
- **Python**: Version 3.8 or higher (Python 3.10, 3.11, or 3.12 recommended).
- **Dependencies** (defined in `requirements.txt`):
  - `Flask>=3.0.0`
  - `Werkzeug>=3.0.0`
  - `qrcode>=7.4.2`

### Supported Operating Systems
- **Windows**: Windows 10, Windows 11, Windows Server (PowerShell / Command Prompt).
- **Linux**: Ubuntu, Debian, Fedora, CentOS, Arch Linux, Alpine, etc.
- **macOS**: macOS 11 (Big Sur) or higher.

### Browser Capabilities & Limitations
- **Core Requirements**: Any modern browser supporting the Fetch API, `AbortController`, `Blob.slice()`, `localStorage`, and ES6 JavaScript (Google Chrome, Microsoft Edge, Mozilla Firefox, Apple Safari, Opera, Brave).
- **Desktop Browsers**: Support selecting individual files, multiple files, and whole directories via `<input webkitdirectory directory>` or directory drag-and-drop.
- **Mobile Browsers**: Mobile operating systems (iOS Safari, Android Chrome) enforce platform-level file picker sandboxing. Directory selection (`webkitdirectory`) is generally disabled or restricted on mobile devices. Mobile users can select and upload multiple individual files simultaneously.

---

## 5. Installation

### Windows (PowerShell)
```powershell
# 1. Clone the repository
git clone https://github.com/SurendiranBJ/QuickShare.git
cd QuickShare

# 2. Create a virtual environment
python -m venv venv

# 3. Activate the virtual environment
.\venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements.txt
```

### Linux / macOS (Bash / Zsh)
```bash
# 1. Clone the repository
git clone https://github.com/SurendiranBJ/QuickShare.git
cd QuickShare

# 2. Create a virtual environment
python3 -m venv venv

# 3. Activate the virtual environment
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

---

## 6. Starting the Application

Start the server by executing:

```bash
python Quickshare.py
```

### Startup Output Example
```text
2026-09-01 16:00:00 [INFO] Starting QuickShare LAN File Transfer Server on http://192.168.1.50:5000 (Listening on 0.0.0.0:5000, debug=False)
 * Serving Flask app 'Quickshare'
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.1.50:5000
```

### Connecting to QuickShare
1. **From the Host Computer**: Open `http://localhost:5000` or `http://127.0.0.1:5000` in your web browser.
2. **From Other LAN Devices (Phones, Tablets, Laptops)**:
   - Ensure the client device is connected to the same Wi-Fi network or local subnet.
   - Open a browser and enter the LAN URL displayed in the server console (e.g., `http://192.168.1.50:5000`).
   - Or click **Connect device** on the host screen and scan the generated QR code with your mobile camera.

---

## 7. Windows Firewall & LAN Connectivity

If client devices on your network cannot connect to the server, check the following network configurations:

1. **Windows Defender Firewall**:
   - When launching Python for the first time, Windows may prompt you to allow network access. Select **Private networks**.
   - If blocked, add an inbound firewall rule for TCP port `5000` in PowerShell (Run as Administrator):
     ```powershell
     New-NetFirewallRule -DisplayName "QuickShare Server" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow
     ```
2. **Router Access Point / Client Isolation**:
   - Many guest Wi-Fi networks and public hotspots enable **AP Isolation** / **Client Isolation**, which blocks devices on the same Wi-Fi from communicating directly with each other. Use a private/home Wi-Fi network or disable AP Isolation in router settings.
3. **VPN & Virtual Adapters**:
   - Active VPN connections or virtualization adapters (e.g., WSL, VMware, VirtualBox) may alter routing or bind IP detection to an internal virtual subnet. Ensure your client devices target the physical LAN IP.
4. **Subnet Matching**:
   - Ensure both host and client devices share the same subnet range (e.g., host `192.168.1.50` and phone `192.168.1.75` on subnet mask `255.255.255.0`).

---

## 8. File Upload

1. Click **Browse files** or drag files directly onto the dropzone.
2. Multiple files can be selected at once.
3. Each file enters the upload queue with a dedicated card displaying filename, total size, chunk count, progress track, transfer speed, and estimated time remaining (ETA).
4. Sliced 8 MB binary chunks upload concurrently (up to 4 chunk workers per active file, with 2 active files processed simultaneously).
5. As chunks complete, the server records progress and verifies chunk integrity.
6. Upon receiving all chunks, the server performs streaming assembly, verifies the cumulative SHA-256 hash, and moves the completed file to `uploads/`.
7. Once finished, the file appears in the **Available Files** list.

---

## 9. Folder Upload

QuickShare provides full directory tree uploads while preserving relative folder hierarchies.

### Folder Structure Example
```text
MyProject/
├── README.md
├── requirements.txt
├── src/
│   ├── main.py
│   └── utils.py
└── assets/
    └── logo.png
```

### Folder Upload Workflow
1. Click **Browse folder** (or drag a folder into the dropzone).
2. The browser scans the directory structure and constructs a relative path manifest (`README.md`, `src/main.py`, `assets/logo.png`).
3. QuickShare initializes a folder session via `POST /folder/upload/start`.
4. An aggregate folder card displays overall directory progress, cumulative transfer speed, finished file count, and an expandable sub-file view.
5. Child files are enqueued and transferred through the standard chunked pipeline into the staging cache.
6. When all sub-files are verified, `POST /folder/upload/complete` atomically moves the entire folder hierarchy into `uploads/MyProject/`.
7. After publication, the folder appears as a single unified folder card in **Available Files**.

---

## 10. Folder Resumption & Interruption Recovery

If an upload is paused or interrupted due to network fluctuations:

1. **Active Session Preservation**: In-flight chunks that were successfully written remain intact in `cache/<folder_id>/`.
2. **Reconciliation**: When resumed, the client queries the server for received chunks and continues transferring only the remaining missing parts.
3. **Browser Security Boundary**: Browsers do not persist low-level operating system file handles across hard page reloads. If the page is reloaded during a folder transfer, browser security requires the user to select the folder again to re-bind the local `File` handles and resume staging reconciliation.

---

## 11. Available Files

The **Available Files** section displays all completed files and directories physically stored in the `uploads/` directory:

- **Source of Truth**: Generated directly from the server filesystem (`os.listdir(UPLOAD_DIR)`).
- **Folder Badges**: Directories are detected via `os.path.isdir()` and displayed with total recursive file count and aggregated byte size (e.g., `Folder · 125.40 MB · 18 files`).
- **Clean State**: When `uploads/` is empty, the interface shows an empty state. No mock or demo files are ever rendered.

---

## 12. File Categories

Files are classified into categories based on their extensions:

| Category | Typical File Extensions |
|---|---|
| **Folders** | Physical directories in `uploads/` |
| **Images** | `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.bmp`, `.ico`, `.tiff`, `.heic` |
| **Videos** | `.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.webm`, `.flv`, `.m4v`, `.3gp` |
| **Audio** | `.mp3`, `.wav`, `.aac`, `.flac`, `.ogg`, `.m4a`, `.wma`, `.opus` |
| **Documents** | `.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.ppt`, `.pptx`, `.txt`, `.rtf`, `.csv`, `.md`, `.epub` |
| **Archives** | `.zip`, `.rar`, `.7z`, `.tar`, `.gz`, `.bz2`, `.xz`, `.iso`, `.dmg` |
| **Code** | `.py`, `.js`, `.ts`, `.html`, `.css`, `.json`, `.xml`, `.c`, `.cpp`, `.java`, `.go`, `.rs`, `.php`, `.sh`, `.sql` |
| **Applications**| `.exe`, `.msi`, `.apk`, `.app`, `.deb`, `.rpm`, `.dmg`, `.bin`, `.jar` |
| **Other** | All unrecognized or extensionless files |

Category pills allow instant horizontal filtering with real-time item counts.

---

## 13. Search

- **Main File Search**: Filters the Available Files table in real time as you type, matching against filenames, directory names, and category descriptions (case-insensitive).
- **Folder Explorer Search**: Filters entries inside the Folder Explorer modal to quickly locate specific nested files or subdirectories.

---

## 14. Folder Explorer

Clicking **Open** on any folder card opens the interactive Folder Explorer modal:

```text
Folder Explorer: MyProject
Breadcrumbs: MyProject / src

[ Folder ] utils/                     2 files · 4.2 KB       [ Open ]
[ Code   ] main.py                    1.8 KB · Sep 01, 2026  [ Download ]

                                    [ Download ZIP ] [ Close ]
```

- **Breadcrumb Trail**: Navigate up and down the directory tree by clicking path segments.
- **Subfolder Exploration**: Drill into nested folders with the **Open** button.
- **Individual File Downloads**: Download single nested files directly without needing to download the full folder.
- **ZIP Download Button**: Trigger an instant streaming ZIP download of the entire folder from within the modal.

---

## 15. File Downloads & HTTP Range Support

- **Direct File Downloads**: Served strictly from `uploads/` using `Flask.send_from_directory`.
- **Nested File Downloads**: Access nested sub-paths via `/download/<folder>/<subpath>/<file>`.
- **HTTP Range Requests (`RFC 7233 / 9110`)**: The server processes `Range: bytes=start-end` headers and responds with `206 Partial Content`. This enables:
  - Video and audio playback seeking in mobile and desktop browsers.
  - Multi-threaded download acceleration in download managers.
  - Resuming interrupted downloads.
- **Receiver Isolation**: Client download cancellations or network drops do not alter or delete server files.

---

## 16. Folder ZIP Downloads

Clicking **ZIP** on a folder generates a compressed archive on-the-fly:

1. The server traverses the folder directory structure inside `uploads/<folder_name>/`.
2. A temporary compressed ZIP archive is constructed inside `cache/`.
3. The archive is streamed to the client in 64 KB chunks (`application/zip`) with `Content-Disposition: attachment; filename="<folder_name>.zip"`.
4. **Safe Resource Cleanup**: The file handle is explicitly closed and the temporary archive is removed from disk in a `finally` block once transmission completes or disconnects.

---

## 17. Collision-Safe Naming

To prevent accidental data loss, QuickShare never silently overwrites existing files or folders:

- If `Project` already exists in `uploads/`, a new upload of `Project` is saved as `Project (1)`. Subsequent collisions become `Project (2)`, `Project (3)`, etc.
- If `document.pdf` exists, subsequent uploads are named `document (1).pdf`, `document (2).pdf`, etc.

---

## 18. Cache Architecture & Lifecycle

All uploads stage temporary data in isolated directories under `cache/`:

```text
cache/
├── <upload_id>/                     # Single-file upload session
│   ├── metadata.json                # Upload parameters and chunk status
│   └── chunks/                      # Sliced binary chunks
│       ├── 000000
│       ├── 000001
│       └── ...
└── folder_<folder_id>/              # Folder upload session
    ├── manifest.json                # Folder structure and file manifest
    └── <upload_id>/                 # Staged sub-file upload sessions
        ├── metadata.json
        └── assembled.bin            # Assembled sub-file awaiting folder publication
```

### Cache Lifecycle Rules
- **Active Upload**: Chunks are written to `cache/<upload_id>/chunks/`.
- **Completion**: File is assembled, verified, and moved to `uploads/`. The cache directory is removed.
- **Cancellation**: Active requests abort, and `cache/<upload_id>/` is purged immediately.
- **Paused / Interrupted**: Cache is preserved for resumption.
- **Abandoned**: A background worker daemon purges caches inactive for longer than `UPLOAD_CACHE_TIMEOUT` (6 hours). Active assemblies (`status == "assembling"`) are strictly protected from expiration.
- **Isolation**: Cache directories are inaccessible to download routes.

---

## 19. Upload State Machine

```text
                   [ Start Upload ]
                          |
                          v
                    +------------+
                    |   QUEUED   |
                    +------------+
                          |
                          | (Worker slot available)
                          v
                    +------------+  User Pause   +------------+
                    | UPLOADING  | ------------> |   PAUSED   |
                    +------------+ <------------ +------------+
                          |          User Resume
                          | (All chunks received)
                          v
                    +------------+
                    | ASSEMBLING |
                    +------------+
                          |
             +------------+------------+
             |                         |
             | Integrity Verified      | Verification Failed / Error
             v                         v
      +-------------+           +-------------+
      |  COMPLETED  |           |    ERROR    |
      +-------------+           +-------------+
```

| State | Description | HTTP Chunk Handling |
|---|---|---|
| `queued` | Waiting for concurrent upload worker slot. | Not started. |
| `uploading` | Actively transferring chunks. | Accepting binary chunks. |
| `paused` | Transfer paused by user; in-flight requests aborted; cache preserved. | Temporarily stopped. |
| `assembling` | All chunks received; incremental SHA-256 validation and streaming assembly in progress. | Chunks rejected (`409 Conflict`). |
| `completed` | Verified and atomically published to `uploads/`. Cache purged. | Finalized. |
| `cancelled` | Upload cancelled; active requests aborted; cache deleted. | Session invalid. |
| `error` | Transfer encountered an unrecoverable error or network failure. | Retries permitted. |

---

## 20. Performance Architecture

| Parameter | Configuration / Value | Purpose |
|---|---|---|
| **Chunk Size** | `DEFAULT_CHUNK_SIZE = 8388608` (8 MB) | Prevents browser memory bloat and HTTP payload limits. |
| **Worker Concurrency** | `UPLOAD_CONCURRENCY = 4` | Saturates Wi-Fi / Ethernet connections via parallel chunk transfers. |
| **File Concurrency** | `MAX_CONCURRENT_FILES = 2` | Balances network bandwidth across multiple queued files. |
| **Assembly Buffer** | `STREAM_BUFFER_SIZE = 1048576` (1 MB) | Streams chunk assembly with minimal RAM usage regardless of file size. |
| **Locking System** | `UploadLockManager` (Per-Upload Mutex) | Thread-safe chunk writes without global lock contention. |
| **Speed Calculation** | Exponential Moving Average (EMA) | Smooths transfer speed and ETA estimates. |

### Real-World LAN Throughput Factors
LAN transfer speeds depend entirely on physical hardware and network conditions:
- **Wi-Fi Generation & Band**: 5 GHz Wi-Fi (802.11ac/ax) delivers substantially higher throughput than 2.4 GHz (802.11n).
- **Ethernet**: Gigabit Ethernet cables provide maximum stability and speed.
- **Router / Access Point**: Processing power and traffic load on the local router.
- **Client & Host Hardware**: Storage write speeds (NVMe SSD vs HDD), CPU, and network interface cards.
- **Signal Quality**: Distance from router, physical obstructions, and channel interference.

---

## 21. Security Model

QuickShare is designed for trusted local network environments (home, office, lab).

### Built-in Security Controls
- **Path Traversal Protection**: All filesystem accesses are validated using `is_safe_path()` with `os.path.commonpath` to ensure requests cannot escape `uploads/` or `cache/`.
- **Filename Sanitization**: Inputs are sanitized using Werkzeug's `secure_filename()` to eliminate path separators and unsafe characters.
- **Session Identifier Validation**: Upload session IDs are validated against strict UUID format (`re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-...')`).
- **Cache Isolation**: Staged chunks inside `cache/` are completely isolated and never exposed through download endpoints.
- **Verified Publication**: Files are published to `uploads/` only after full assembly and SHA-256 verification.

### Security Scope & Deployment Notice
QuickShare does not include user authentication, transport-layer encryption (HTTPS), or rate limiting out of the box. It is intended for use on secure, private local networks. If hosting QuickShare on an untrusted or public network, place it behind a reverse proxy (such as Nginx or Caddy) configured with HTTPS and HTTP Basic Authentication.

---

## 22. Configuration

All server parameters can be customized using environment variables:

| Variable | Default | Type | Description | Example |
|---|---|---|---|---|
| `HOST` | `0.0.0.0` | String | Network interface IP to bind. `0.0.0.0` listens on all available LAN interfaces. | `HOST=192.168.1.50` |
| `PORT` | `5000` | Integer | TCP port to listen on. | `PORT=8080` |
| `DEBUG` | `false` | Boolean | Enables Flask debug mode (`true` or `false`). Set to `false` for normal use. | `DEBUG=false` |
| `DEFAULT_CHUNK_SIZE` | `8388608` (8 MB) | Integer (Bytes) | Size of binary chunk slices. | `DEFAULT_CHUNK_SIZE=4194304` |
| `UPLOAD_CACHE_TIMEOUT` | `21600` (6 hours) | Integer (Seconds) | Duration before inactive, abandoned staging cache is deleted. | `UPLOAD_CACHE_TIMEOUT=3600` |
| `CLEANUP_INTERVAL` | `1800` (30 mins) | Integer (Seconds) | Frequency of the background cache cleanup sweep. | `CLEANUP_INTERVAL=900` |

---

## 23. REST API Documentation

### Upload Endpoints

#### 1. Initialize Single File Upload
- **URL**: `POST /upload/start`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "filename": "document.pdf",
    "total_size": 10485760,
    "chunk_size": 8388608
  }
  ```
- **Response (201 Created)**:
  ```json
  {
    "success": true,
    "upload_id": "c8a4d712-4215-46eb-8e27-5d2983b63294",
    "chunk_size": 8388608,
    "total_chunks": 2,
    "status": "uploading"
  }
  ```

#### 2. Upload Binary Chunk
- **URL**: `POST /upload/chunk`
- **Headers**: `Content-Type: multipart/form-data`
- **Form Data**:
  - `upload_id`: `c8a4d712-4215-46eb-8e27-5d2983b63294`
  - `chunk_index`: `0`
  - `total_chunks`: `2`
  - `chunk`: `(binary data)`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "upload_id": "c8a4d712-4215-46eb-8e27-5d2983b63294",
    "chunk_index": 0,
    "received_count": 1
  }
  ```

#### 3. Complete File Upload
- **URL**: `POST /upload/complete`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "upload_id": "c8a4d712-4215-46eb-8e27-5d2983b63294"
  }
  ```
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "filename": "document.pdf",
    "size": 10485760,
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```

#### 4. Query Upload Status
- **URL**: `GET /upload/status/<upload_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "upload_id": "c8a4d712-4215-46eb-8e27-5d2983b63294",
    "filename": "document.pdf",
    "total_size": 10485760,
    "chunk_size": 8388608,
    "total_chunks": 2,
    "received_chunks": [0],
    "status": "uploading"
  }
  ```

#### 5. Cancel File Upload
- **URL**: `POST /upload/cancel/<upload_id>` or `DELETE /upload/<upload_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "message": "Upload cancelled and cache purged"
  }
  ```

---

### Folder Endpoints

#### 6. Initialize Folder Upload
- **URL**: `POST /folder/upload/start`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "folder_name": "MyProject",
    "total_files": 2,
    "total_size": 20480,
    "files": [
      { "relative_path": "README.md", "size": 1024 },
      { "relative_path": "src/main.py", "size": 19456 }
    ]
  }
  ```
- **Response (201 Created)**:
  ```json
  {
    "success": true,
    "folder_id": "f5e6a123-8765-4321-abcd-ef0123456789",
    "folder_name": "MyProject",
    "total_files": 2,
    "total_size": 20480
  }
  ```

#### 7. Finalize & Publish Folder
- **URL**: `POST /folder/upload/complete`
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "folder_id": "f5e6a123-8765-4321-abcd-ef0123456789"
  }
  ```
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "folder_name": "MyProject",
    "total_files": 2,
    "total_size": 20480,
    "message": "Folder MyProject uploaded and published successfully"
  }
  ```

#### 8. Cancel Folder Upload
- **URL**: `POST /folder/upload/cancel/<folder_id>` or `DELETE /folder/upload/cancel/<folder_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "message": "Folder upload cancelled and cache purged"
  }
  ```

#### 9. Query Folder Upload Status
- **URL**: `GET /folder/status/<folder_id>`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "folder_id": "f5e6a123-8765-4321-abcd-ef0123456789",
    "folder_name": "MyProject",
    "total_files": 2,
    "completed_files": 1,
    "status": "uploading"
  }
  ```

#### 10. Browse Folder Contents
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
      {
        "name": "src",
        "relative_path": "MyProject/src",
        "is_folder": true,
        "file_count": 1,
        "size": 19456,
        "size_str": "19.0 KB",
        "type_info": { "category": "folders", "label": "Folder · 1 files", "badge_class": "file-folder", "icon": "folder" }
      },
      {
        "name": "README.md",
        "relative_path": "MyProject/README.md",
        "is_folder": false,
        "size": 1024,
        "size_str": "1.0 KB",
        "type_info": { "category": "documents", "label": "Markdown Document", "badge_class": "file-document", "icon": "document" }
      }
    ]
  }
  ```

---

### Download & Discovery Endpoints

#### 11. Download File (Direct or Nested)
- **URL**: `GET /download/<path:filepath>`
- **Headers (Optional)**: `Range: bytes=0-1048575`
- **Response**: Binary file stream (`200 OK` or `206 Partial Content`).

#### 12. Download Folder as ZIP
- **URL**: `GET /download/zip/<path:folder_name>`
- **Response**: Streaming ZIP archive (`application/zip`) with `Content-Disposition: attachment; filename="<folder_name>.zip"`.

#### 13. List Available Files (JSON API)
- **URL**: `GET /api/files` or `GET /files`
- **Response (200 OK)**:
  ```json
  {
    "success": true,
    "count": 2,
    "files": [
      {
        "name": "MyProject",
        "type": "folder",
        "is_folder": true,
        "file_count": 2,
        "size": 20480,
        "size_str": "20.0 KB",
        "mtime": 1756720000.0,
        "mtime_str": "Sep 01, 2026",
        "type_info": { "category": "folders", "label": "Folder · 2 files", "badge_class": "file-folder", "icon": "folder" }
      },
      {
        "name": "document.pdf",
        "type": "file",
        "is_folder": false,
        "size": 10485760,
        "size_str": "10.0 MB",
        "mtime": 1756719000.0,
        "mtime_str": "Sep 01, 2026",
        "type_info": { "category": "documents", "label": "PDF Document", "badge_class": "file-document", "icon": "document" }
      }
    ]
  }
  ```

#### 14. LAN Connection QR Code
- **URL**: `GET /qr`
- **Response**: Self-contained SVG image (`image/svg+xml`) encoding the server's LAN access URL.

#### 15. Web Application Homepage
- **URL**: `GET /`
- **Response**: HTML interface with embedded CSS, JavaScript, and initial file listing.

---

## 24. Directory Structure

```text
QuickShare/
├── Quickshare.py          # Primary Flask server, REST API, templates, and client JS
├── requirements.txt       # Dependencies (Flask, Werkzeug, qrcode)
├── README.md              # Complete operational and architectural manual
├── .gitignore             # Excludes runtime storage, virtual environments, and caches
├── uploads/               # Verified completed files and folders (generated at runtime)
└── cache/                 # Staged binary chunks and manifests (generated at runtime)
```

---

## 25. Git Ignore Guidelines

Runtime data, test scripts, and local caches are strictly ignored by `.gitignore`:
- `uploads/` (User shared files and folders)
- `cache/` (In-progress binary chunks and temporary archives)
- `venv/`, `env/`, `.env` (Python virtual environments and environment configurations)
- `__pycache__/`, `*.pyc` (Compiled Python bytecode)
- `scratch/`, `test_*.py` (Local testing and development scripts)

---

## 26. Testing Strategy

QuickShare can be verified through automated regression suites, manual browser checks, and real-device LAN tests.

### 1. Automated Test Suites
Automated tests use Python's `unittest` module and temporary sandbox directories (`tempfile.mkdtemp()`) to avoid polluting the workspace `uploads/` directory:
- **Discovery Tests**: Validates that physical files and directories in `uploads/` appear accurately with correct sizes and category metadata.
- **Upload Lifecycle Tests**: Validates chunk generation, chunk upload, assembly, incremental SHA-256 verification, and atomic publishing.
- **Folder Pipeline Tests**: Validates folder manifest parsing, nested sub-file assembly, and folder publication.
- **Security Tests**: Verifies rejection of path traversal sequences (`../`, `/etc/passwd`).
- **ZIP Download Tests**: Validates dynamic ZIP generation and temporary file cleanup.

### 2. Manual Browser Tests
- Validate drag-and-drop file and folder queuing.
- Check pause, resume, cancel, and retry button interactions.
- Test Folder Explorer breadcrumbs and subfolder navigation.
- Verify real-time search and category filtering.

---

## 27. Real LAN Test Procedure

To verify behavior across physical devices on your local network:

1. **Host Setup**:
   - Start QuickShare: `python Quickshare.py`
   - Note the LAN URL (e.g., `http://192.168.1.50:5000`).
2. **Client Device (Phone / Tablet / Laptop)**:
   - Connect client device to the same Wi-Fi network.
   - Open browser or scan the host's QR code via **Connect device**.
3. **Run Test Scenarios**:
   - **Single File Upload**: Upload a photo or PDF from phone to laptop.
   - **Multi-File Upload**: Select 5 files simultaneously and observe queue scheduling.
   - **Folder Upload**: Upload a folder from laptop and verify preservation on server.
   - **Folder Explorer**: Open the folder on the phone, navigate subdirectories, and download a single nested file.
   - **Folder ZIP**: Download the entire folder as a single ZIP archive.
   - **Media Seeking**: Play an uploaded video directly in the mobile browser and seek across the timeline (verifying HTTP Range `206 Partial Content`).
   - **Pause & Resume**: Pause an active transfer, wait 10 seconds, resume, and confirm completion.

---

## 28. Troubleshooting

### 1. Phone or other device cannot connect to server
- **Check Wi-Fi Network**: Ensure both devices are connected to the exact same Wi-Fi SSID.
- **Windows Firewall**: Add an inbound firewall rule for port `5000` (see Section 7).
- **AP Isolation**: Confirm your router does not have Client / AP Isolation enabled.
- **Check Host LAN IP**: Ensure the IP address in your browser matches the current host LAN IP shown in the server console.

### 2. QR code does not open the page
- Ensure the phone camera app can read standard URLs.
- Confirm the phone is connected to Wi-Fi rather than cellular mobile data.

### 3. Folder picker does not appear
- Ensure you are using a desktop browser (Chrome, Edge, Firefox, Safari) that supports `<input webkitdirectory>`.
- On mobile devices, directory selection APIs are restricted by the OS; select multiple individual files instead.

### 4. Upload does not start or appears stuck
- Check browser console (F12) for network errors.
- Ensure the server host is running and reachable.
- Verify storage permissions for the `cache/` directory.

### 5. Upload speed is lower than expected
- Switch from 2.4 GHz Wi-Fi to 5 GHz Wi-Fi or Gigabit Ethernet.
- Reduce distance between client device and Wi-Fi router.
- Check whether background downloads or VPN clients are saturating the local network.

### 6. Resuming after page reload prompts for file
- Browsers cannot retain raw file system handles after a hard refresh for security reasons. Select the matching file when prompted to resume from where the upload left off.

### 7. Port 5000 is already in use
- Specify a custom port via environment variable:
  ```powershell
  $env:PORT="8080"; python Quickshare.py
  ```

### 8. Available Files is empty after uploading
- Check server logs to ensure the upload finished assembly and SHA-256 verification.
- Incomplete uploads remain isolated inside `cache/` until finalized.

### 9. ZIP download fails on very large folders
- Ensure the host drive containing `cache/` has sufficient free disk space to build the temporary ZIP archive.

---

## 29. Browser Limitations

- **File Handle Persistence**: Browsers do not allow web scripts to retain direct file access across page refreshes. When a page is reloaded during an upload, selecting the file again re-links the data to the existing session.
- **Background Tab Throttling**: Mobile browsers (especially iOS Safari) throttle JavaScript execution and close network sockets when a tab is backgrounded or the screen locks. Keep the browser tab active during large transfers.
- **Mobile Directory Selection**: Mobile operating systems do not expose directory picker dialogs to web browsers. Mobile users can select and upload multiple files simultaneously.

---

## 30. Frequently Asked Questions (FAQ)

#### Is QuickShare cloud-based?
No. QuickShare runs entirely on your local machine. Files transfer directly between devices across your local network.

#### Can I use QuickShare without an internet connection?
Yes. QuickShare requires only a local Wi-Fi or Ethernet connection between devices. No active internet access is needed.

#### Can I transfer files from a phone to a laptop?
Yes. Open the server URL on your phone's browser, select files, and upload them directly to the host laptop.

#### Can I upload entire folders?
Yes. Using a desktop browser, click **Browse folder** or drag and drop a folder to upload the entire directory structure.

#### Can I download a single file from an uploaded folder?
Yes. Click **Open** on the folder card in Available Files to open the Folder Explorer modal and download individual nested files.

#### Can I download an entire folder at once?
Yes. Click **ZIP** on the folder card or **Download ZIP** inside the Folder Explorer to download the folder as a single ZIP archive.

#### How many files can upload simultaneously?
QuickShare uploads up to 2 files concurrently (`MAX_CONCURRENT_FILES = 2`), with 4 parallel chunk workers per file (`UPLOAD_CONCURRENCY = 4`). Additional files wait in the queue and start automatically as slots open.

#### Can interrupted uploads resume?
Yes. QuickShare slices files into 8 MB chunks. If interrupted, transfer resumes from the first missing chunk without re-uploading completed chunks.

#### Where are completed files stored?
Completed files and folders are stored in the `uploads/` directory inside the application folder on the host computer.

#### What happens if I cancel an upload?
In-flight network requests are aborted immediately and the server purges all staged chunks from `cache/`.

#### Are incomplete files visible in Available Files?
No. In-progress uploads remain isolated in `cache/` and appear in `uploads/` only after verification and assembly.

#### Why is LAN transfer speed lower than theoretical Wi-Fi limits?
Real-world throughput is affected by Wi-Fi band (2.4 GHz vs 5 GHz), physical obstructions, distance from router, disk write speeds, and router CPU capabilities.

---

## 31. License

This project is licensed under the MIT License. You are free to use, modify, and distribute it in accordance with the license terms.
