import os
import sys
import io
import time
import uuid
import json
import shutil
import hashlib
import re
import math
import socket
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from flask import Flask, request, send_from_directory, render_template_string, jsonify, abort, Response
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
# Configuration Constants (Configurable via Environment Variables)
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

DEFAULT_CHUNK_SIZE = int(os.getenv("DEFAULT_CHUNK_SIZE", 5 * 1024 * 1024))  # 5 MB default
UPLOAD_CACHE_TIMEOUT = int(os.getenv("UPLOAD_CACHE_TIMEOUT", 21600))        # 6 hours default
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 1800))                 # 30 mins scan interval
STREAM_BUFFER_SIZE = 1024 * 1024                                            # 1 MB streaming assembly buffer
UUID_REGEX = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.IGNORECASE)

# Ensure required directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)
# Allow large chunk payloads (up to 100MB per chunk if configured)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# -----------------------------------------------------------------------------
# In-Memory Fast State Cache & Per-Upload Lock Manager
# -----------------------------------------------------------------------------
class UploadLockManager:
    """
    Provides thread-safe per-upload locks without global contention or memory leaks.
    Each upload_id has an isolated mutex with reference counting.
    """
    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}  # upload_id -> [Lock, ref_count]
        self._filename_collision_lock = threading.Lock()
        self._memory_metadata = {}  # upload_id -> metadata dict

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

    def get_cached_meta(self, upload_id):
        with self._guard:
            return self._memory_metadata.get(upload_id)

    def set_cached_meta(self, upload_id, metadata):
        with self._guard:
            self._memory_metadata[upload_id] = metadata

    def remove_cached_meta(self, upload_id):
        with self._guard:
            self._memory_metadata.pop(upload_id, None)

upload_lock_manager = UploadLockManager()

# -----------------------------------------------------------------------------
# Helper Functions, LAN IP Discovery & Unified File Classification
# -----------------------------------------------------------------------------
def get_lan_ip():
    """Dynamically detects the primary LAN IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Connect to a public DNS IP (no packet is transmitted over the wire)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            _, _, ips = socket.gethostbyname_ex(socket.gethostname())
            for ip in ips:
                if not ip.startswith('127.'):
                    return ip
            return '127.0.0.1'
        except Exception:
            return '127.0.0.1'

def generate_qr_svg(url):
    """Generates a clean SVG QR code string."""
    try:
        import qrcode
        import qrcode.image.svg
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=1,
            image_factory=qrcode.image.svg.SvgPathImage
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        out = io.BytesIO()
        img.save(out)
        return out.getvalue().decode('utf-8')
    except Exception as e:
        logger.warning(f"QR code generation failed or qrcode package unavailable: {e}")
        return None

def format_bytes(size_bytes):
    """Format bytes into a clean human readable string."""
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

def get_file_type_info(filename):
    """
    Unified Single Source of Truth for File Type Detection:
    Returns (category, label, badge_class, icon) based on filename extension.
    Categories: 'images', 'videos', 'audio', 'documents', 'archives', 'code', 'applications', 'other'.
    Handles case-insensitivity, multiple dots, and files without extensions.
    """
    if not filename:
        return {
            "category": "other",
            "label": "File",
            "badge_class": "file-generic",
            "icon": "generic"
        }
    
    lower = filename.lower()
    ext = ""
    if lower.endswith(".tar.gz"):
        ext = "tar.gz"
    elif lower.endswith(".tar.bz2"):
        ext = "tar.bz2"
    elif lower.endswith(".tar.xz"):
        ext = "tar.xz"
    else:
        parts = lower.rsplit(".", 1)
        if len(parts) > 1:
            ext = parts[1]

    EXT_MAP = {
        # Images
        "jpg": ("images", "Image · JPG", "file-image", "image"),
        "jpeg": ("images", "Image · JPEG", "file-image", "image"),
        "png": ("images", "Image · PNG", "file-image", "image"),
        "gif": ("images", "Image · GIF", "file-image", "image"),
        "webp": ("images", "Image · WebP", "file-image", "image"),
        "bmp": ("images", "Image · BMP", "file-image", "image"),
        "svg": ("images", "Vector · SVG", "file-image", "image"),
        "tiff": ("images", "Image · TIFF", "file-image", "image"),
        "tif": ("images", "Image · TIF", "file-image", "image"),
        "ico": ("images", "Icon · ICO", "file-image", "image"),
        "avif": ("images", "Image · AVIF", "file-image", "image"),
        
        # Videos
        "mp4": ("videos", "Video · MP4", "file-video", "video"),
        "mkv": ("videos", "Video · MKV", "file-video", "video"),
        "mov": ("videos", "Video · QuickTime", "file-video", "video"),
        "avi": ("videos", "Video · AVI", "file-video", "video"),
        "webm": ("videos", "Video · WebM", "file-video", "video"),
        "flv": ("videos", "Video · FLV", "file-video", "video"),
        "wmv": ("videos", "Video · WMV", "file-video", "video"),
        "m4v": ("videos", "Video · M4V", "file-video", "video"),
        "mpeg": ("videos", "Video · MPEG", "file-video", "video"),
        "mpg": ("videos", "Video · MPG", "file-video", "video"),
        "3gp": ("videos", "Video · 3GP", "file-video", "video"),
        
        # Audio
        "mp3": ("audio", "Audio · MP3", "file-audio", "audio"),
        "wav": ("audio", "Audio · WAV", "file-audio", "audio"),
        "flac": ("audio", "Audio · FLAC", "file-audio", "audio"),
        "aac": ("audio", "Audio · AAC", "file-audio", "audio"),
        "m4a": ("audio", "Audio · M4A", "file-audio", "audio"),
        "ogg": ("audio", "Audio · OGG", "file-audio", "audio"),
        "wma": ("audio", "Audio · WMA", "file-audio", "audio"),
        "opus": ("audio", "Audio · OPUS", "file-audio", "audio"),
        
        # Documents (PDF, Office, Text, Markdown)
        "pdf": ("documents", "PDF Document", "file-pdf", "pdf"),
        "txt": ("documents", "Plain Text", "file-text", "text"),
        "md": ("documents", "Markdown Document", "file-text", "text"),
        "markdown": ("documents", "Markdown Document", "file-text", "text"),
        "log": ("documents", "Log File", "file-text", "text"),
        "rtf": ("documents", "Rich Text Document", "file-text", "text"),
        "doc": ("documents", "Word Document", "file-doc", "doc"),
        "docx": ("documents", "Word Document", "file-doc", "doc"),
        "odt": ("documents", "OpenDocument Text", "file-doc", "doc"),
        "xls": ("documents", "Excel Spreadsheet", "file-sheet", "sheet"),
        "xlsx": ("documents", "Excel Spreadsheet", "file-sheet", "sheet"),
        "csv": ("documents", "CSV Spreadsheet", "file-sheet", "sheet"),
        "ods": ("documents", "OpenDocument Spreadsheet", "file-sheet", "sheet"),
        "ppt": ("documents", "PowerPoint Presentation", "file-pres", "pres"),
        "pptx": ("documents", "PowerPoint Presentation", "file-pres", "pres"),
        "odp": ("documents", "OpenDocument Presentation", "file-pres", "pres"),
        
        # Archives
        "zip": ("archives", "ZIP Archive", "file-archive", "archive"),
        "rar": ("archives", "RAR Archive", "file-archive", "archive"),
        "7z": ("archives", "7Z Archive", "file-archive", "archive"),
        "tar": ("archives", "TAR Archive", "file-archive", "archive"),
        "gz": ("archives", "GZ Archive", "file-archive", "archive"),
        "bz2": ("archives", "BZ2 Archive", "file-archive", "archive"),
        "xz": ("archives", "XZ Archive", "file-archive", "archive"),
        "tar.gz": ("archives", "Tarball Archive", "file-archive", "archive"),
        "tar.bz2": ("archives", "Tarball Archive", "file-archive", "archive"),
        "tar.xz": ("archives", "Tarball Archive", "file-archive", "archive"),
        
        # Code & Scripts
        "py": ("code", "Python Script", "file-code", "code"),
        "js": ("code", "JavaScript Source", "file-code", "code"),
        "jsx": ("code", "React JSX Source", "file-code", "code"),
        "ts": ("code", "TypeScript Source", "file-code", "code"),
        "tsx": ("code", "React TSX Source", "file-code", "code"),
        "html": ("code", "HTML Document", "file-code", "code"),
        "htm": ("code", "HTML Document", "file-code", "code"),
        "css": ("code", "CSS Stylesheet", "file-code", "code"),
        "java": ("code", "Java Source", "file-code", "code"),
        "cpp": ("code", "C++ Source", "file-code", "code"),
        "c": ("code", "C Source", "file-code", "code"),
        "h": ("code", "C/C++ Header", "file-code", "code"),
        "hpp": ("code", "C++ Header", "file-code", "code"),
        "go": ("code", "Go Source", "file-code", "code"),
        "rs": ("code", "Rust Source", "file-code", "code"),
        "php": ("code", "PHP Script", "file-code", "code"),
        "rb": ("code", "Ruby Script", "file-code", "code"),
        "sh": ("code", "Shell Script", "file-code", "code"),
        "bat": ("code", "Batch Script", "file-code", "code"),
        "ps1": ("code", "PowerShell Script", "file-code", "code"),
        "json": ("code", "JSON File", "file-code", "code"),
        "xml": ("code", "XML File", "file-code", "code"),
        "yaml": ("code", "YAML File", "file-code", "code"),
        "yml": ("code", "YAML File", "file-code", "code"),
        "sql": ("code", "SQL Database Script", "file-code", "code"),
        
        # Applications / Executables
        "exe": ("applications", "Application · EXE", "file-exe", "exe"),
        "msi": ("applications", "Windows Installer", "file-exe", "exe"),
        "apk": ("applications", "Android Package", "file-exe", "exe"),
        "dmg": ("applications", "macOS Disk Image", "file-exe", "exe"),
        "deb": ("applications", "Debian Package", "file-exe", "exe"),
        "rpm": ("applications", "RPM Package", "file-exe", "exe"),
        "app": ("applications", "macOS Application", "file-exe", "exe"),
        
        # Design / Other
        "psd": ("other", "Photoshop Document", "file-design", "design"),
        "ai": ("other", "Illustrator Artwork", "file-design", "design"),
        "sketch": ("other", "Sketch Design", "file-design", "design"),
        "fig": ("other", "Figma Design", "file-design", "design"),
    }

    if ext in EXT_MAP:
        cat, label, bg, icon = EXT_MAP[ext]
        return {"category": cat, "label": label, "badge_class": bg, "icon": icon}
    
    # Fallback to other
    display_ext = ext.upper() if ext else "FILE"
    return {
        "category": "other",
        "label": f"File · {display_ext}" if ext else "Generic File",
        "badge_class": "file-generic",
        "icon": "generic"
    }

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
    """Safely load metadata.json for an upload session, utilizing in-memory cache when available."""
    cached = upload_lock_manager.get_cached_meta(upload_id)
    if cached is not None:
        return cached

    meta_path = get_metadata_path(upload_id)
    if not meta_path or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data["received_chunks"] = set(data.get("received_chunks", []))
            upload_lock_manager.set_cached_meta(upload_id, data)
            return data
    except Exception as e:
        logger.error(f"Error reading metadata for upload_id={upload_id}: {e}")
        return None

def save_metadata(upload_id, metadata, write_disk=True):
    """Save metadata to in-memory state and atomically write to disk without fsync bottlenecks."""
    upload_lock_manager.set_cached_meta(upload_id, metadata)
    if not write_disk:
        return True

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
    """
    Scans cache directory and cleans up abandoned uploads safely with lock.
    Assembling uploads are strictly protected from ordinary cleanup to prevent deleting active multi-GB assembly.
    """
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
                    upload_lock_manager.remove_cached_meta(entry)
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

# Start background cleanup thread safely
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    cleanup_thread = threading.Thread(target=cache_cleanup_worker, daemon=True, name="CacheCleanupWorker")
    cleanup_thread.start()

# -----------------------------------------------------------------------------
# Frontend HTML / UI Template (Unified Connect Device + File Category Filters)
# -----------------------------------------------------------------------------
UPLOAD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, viewport-fit=cover">
    <title>QuickShare — Local Network File Transfer</title>
    <style>
        :root {
            --bg-canvas: #090a0f;
            --bg-surface: #111318;
            --bg-surface-elevated: #171922;
            --bg-surface-hover: #1c202a;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-strong: rgba(255, 255, 255, 0.16);
            --text-primary: #f0f2f7;
            --text-secondary: #8c93a4;
            --text-tertiary: #545b6d;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --accent-subtle: rgba(59, 130, 246, 0.12);
            --accent-text: #60a5fa;
            --status-success: #10b981;
            --status-warning: #f59e0b;
            --status-danger: #ef4444;
            --radius-sm: 6px;
            --radius-md: 10px;
            --radius-lg: 14px;
            --font-stack: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --transition-smooth: all 0.18s cubic-bezier(0.16, 1, 0.3, 1);
        }

        *, *::before, *::after {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: var(--font-stack);
            -webkit-tap-highlight-color: transparent;
        }

        html, body {
            width: 100%;
            max-width: 100%;
            margin: 0;
            padding: 0;
            overflow-x: hidden;
            background-color: var(--bg-canvas);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }

        /* Top Application Navigation */
        .app-nav {
            width: 100%;
            border-bottom: 1px solid var(--border-subtle);
            background-color: rgba(9, 10, 15, 0.9);
            backdrop-filter: blur(12px);
            position: sticky;
            top: 0;
            z-index: 50;
        }

        .nav-inner {
            width: 100%;
            max-width: 860px;
            margin: 0 auto;
            padding: 0.75rem 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            flex-wrap: wrap;
        }

        .brand-group {
            display: flex;
            align-items: center;
            gap: 0.55rem;
            text-decoration: none;
            color: var(--text-primary);
            flex-shrink: 0;
        }

        .brand-icon {
            width: 22px;
            height: 22px;
            color: var(--accent);
            flex-shrink: 0;
        }

        .brand-title {
            font-size: 0.98rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .brand-badge {
            font-size: 0.65rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            background: var(--bg-surface-elevated);
            color: var(--text-tertiary);
            padding: 0.15rem 0.4rem;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border-subtle);
        }

        .nav-actions {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
        }

        .lan-pill-btn {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 0.38rem 0.65rem;
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            color: var(--text-secondary);
            font-size: 0.78rem;
            font-weight: 500;
            cursor: pointer;
            transition: var(--transition-smooth);
            text-decoration: none;
        }

        .lan-pill-btn:hover, .lan-pill-btn:focus-visible {
            background: var(--bg-surface-hover);
            border-color: var(--border-strong);
            color: var(--text-primary);
            outline: none;
        }

        .status-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background-color: var(--status-success);
            box-shadow: 0 0 6px var(--status-success);
            flex-shrink: 0;
        }

        .lan-ip-text {
            font-family: var(--font-mono);
            font-size: 0.78rem;
            color: var(--text-primary);
        }

        /* Main Workspace Container */
        .workspace {
            width: 100%;
            max-width: 860px;
            padding: 1.75rem 1rem 4rem;
            display: flex;
            flex-direction: column;
            gap: 2rem;
            margin: 0 auto;
        }

        /* Section Titles */
        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 0.75rem;
            gap: 0.5rem;
            flex-wrap: wrap;
        }

        .section-title {
            font-size: 0.78rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-tertiary);
        }

        /* Dropzone Component */
        .dropzone-container {
            width: 100%;
            border: 1px dashed var(--border-strong);
            border-radius: var(--radius-lg);
            background: var(--bg-surface);
            padding: 2.75rem 1.25rem;
            text-align: center;
            cursor: pointer;
            transition: var(--transition-smooth);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1rem;
        }

        .dropzone-container:hover, .dropzone-container.dragover, .dropzone-container:focus-visible {
            border-color: var(--accent);
            background: var(--accent-subtle);
            outline: none;
        }

        .dropzone-icon-box {
            width: 44px;
            height: 44px;
            border-radius: var(--radius-md);
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text-secondary);
            transition: var(--transition-smooth);
        }

        .dropzone-container:hover .dropzone-icon-box, .dropzone-container.dragover .dropzone-icon-box {
            color: var(--accent-text);
            border-color: rgba(59, 130, 246, 0.35);
        }

        .dropzone-text-group {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }

        .dropzone-title {
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--text-primary);
            letter-spacing: -0.01em;
        }

        .dropzone-subtitle {
            font-size: 0.82rem;
            color: var(--text-secondary);
        }

        .desktop-text { display: inline; }
        .mobile-text { display: none; }

        .file-input { display: none; }

        /* Buttons */
        .btn {
            background: var(--bg-surface-elevated);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 0.42rem 0.8rem;
            font-size: 0.82rem;
            font-weight: 500;
            cursor: pointer;
            transition: var(--transition-smooth);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.45rem;
            text-decoration: none;
            min-height: 36px;
            white-space: nowrap;
        }

        .btn:hover, .btn:focus-visible {
            background: var(--bg-surface-hover);
            border-color: var(--border-strong);
            outline: none;
        }

        .btn-primary {
            background: var(--accent);
            color: #ffffff;
            border-color: var(--accent);
            font-weight: 600;
        }

        .btn-primary:hover, .btn-primary:focus-visible {
            background: var(--accent-hover);
            border-color: var(--accent-hover);
        }

        .btn-danger {
            background: rgba(239, 68, 68, 0.1);
            color: #f87171;
            border-color: rgba(239, 68, 68, 0.25);
        }

        .btn-danger:hover, .btn-danger:focus-visible {
            background: rgba(239, 68, 68, 0.2);
            border-color: rgba(239, 68, 68, 0.4);
        }

        .btn-sm {
            padding: 0.35rem 0.65rem;
            font-size: 0.78rem;
            min-height: 32px;
        }

        .btn-icon-only {
            padding: 0.4rem;
            width: 32px;
            height: 32px;
            min-height: 32px;
        }

        /* SVG Icon helper */
        .svg-icon {
            width: 15px;
            height: 15px;
            stroke-width: 2;
            stroke: currentColor;
            fill: none;
            stroke-linecap: round;
            stroke-linejoin: round;
            flex-shrink: 0;
        }

        /* Transfer Queue */
        .transfer-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            width: 100%;
        }

        .transfer-card {
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            padding: 1.1rem;
            display: flex;
            flex-direction: column;
            gap: 0.8rem;
            width: 100%;
            transition: var(--transition-smooth);
        }

        .transfer-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 0.75rem;
            flex-wrap: wrap;
        }

        .transfer-title-group {
            display: flex;
            flex-direction: column;
            gap: 0.2rem;
            min-width: 0;
            flex: 1 1 200px;
        }

        .transfer-filename {
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--text-primary);
            overflow-wrap: anywhere;
            word-break: break-word;
            line-height: 1.35;
        }

        .transfer-meta {
            font-size: 0.76rem;
            color: var(--text-secondary);
            font-family: var(--font-mono);
        }

        .transfer-actions {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            flex-shrink: 0;
        }

        /* Semantic Badges */
        .status-badge {
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.18rem 0.45rem;
            border-radius: var(--radius-sm);
            letter-spacing: 0.04em;
            text-transform: uppercase;
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            font-family: var(--font-mono);
        }

        .badge-uploading { background: var(--accent-subtle); color: var(--accent-text); }
        .badge-assembling { background: rgba(245, 158, 11, 0.12); color: #fbbf24; }
        .badge-completed { background: rgba(16, 185, 129, 0.12); color: #34d399; }
        .badge-cancelled { background: rgba(239, 68, 68, 0.12); color: #f87171; }
        .badge-error { background: rgba(239, 68, 68, 0.12); color: #f87171; }
        .badge-paused { background: rgba(140, 147, 164, 0.12); color: var(--text-secondary); }

        /* Progress Bar */
        .progress-track {
            width: 100%;
            height: 4px;
            background-color: var(--bg-surface-elevated);
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            background-color: var(--accent);
            border-radius: 999px;
            width: 0%;
            transition: width 0.2s ease-out;
        }

        .progress-fill.completed {
            background-color: var(--status-success);
        }

        .transfer-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.76rem;
            color: var(--text-secondary);
            font-family: var(--font-mono);
            flex-wrap: wrap;
            gap: 0.4rem;
        }

        /* Interrupted Session Notification */
        .session-resume-alert {
            width: 100%;
            background: var(--bg-surface);
            border: 1px solid rgba(245, 158, 11, 0.3);
            border-radius: var(--radius-md);
            padding: 0.85rem 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1rem;
            flex-wrap: wrap;
        }

        .session-resume-text {
            font-size: 0.82rem;
            color: var(--text-primary);
            line-height: 1.4;
            flex: 1 1 240px;
        }

        .session-resume-actions {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }

        /* Available Files Section & Filter System */
        .files-container {
            width: 100%;
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            overflow: hidden;
        }

        .files-toolbar {
            width: 100%;
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .toolbar-top-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            width: 100%;
        }

        .search-box {
            position: relative;
            display: flex;
            align-items: center;
            width: 100%;
        }

        .search-icon {
            position: absolute;
            left: 0.65rem;
            color: var(--text-tertiary);
            pointer-events: none;
            width: 14px;
            height: 14px;
        }

        .search-input {
            width: 100%;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 0.45rem 0.65rem 0.45rem 2.1rem;
            color: var(--text-primary);
            font-size: 0.82rem;
            outline: none;
            transition: var(--transition-smooth);
        }

        .search-input:focus {
            border-color: var(--accent);
            background: var(--bg-surface-hover);
        }

        /* Category Filter Pills */
        .filter-pills-bar {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            overflow-x: auto;
            width: 100%;
            scrollbar-width: none;
            -ms-overflow-style: none;
            padding: 0.15rem 0;
        }

        .filter-pills-bar::-webkit-scrollbar {
            display: none;
        }

        .filter-pill {
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            color: var(--text-secondary);
            padding: 0.28rem 0.65rem;
            font-size: 0.78rem;
            font-weight: 500;
            cursor: pointer;
            transition: var(--transition-smooth);
            white-space: nowrap;
            flex-shrink: 0;
        }

        .filter-pill:hover, .filter-pill:focus-visible {
            background: var(--bg-surface-hover);
            border-color: var(--border-strong);
            color: var(--text-primary);
            outline: none;
        }

        .filter-pill.active {
            background: var(--accent);
            color: #ffffff;
            border-color: var(--accent);
            font-weight: 600;
        }

        .file-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }

        .file-table th {
            text-align: left;
            padding: 0.65rem 1rem;
            color: var(--text-tertiary);
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border-bottom: 1px solid var(--border-subtle);
            background: var(--bg-surface-elevated);
        }

        .file-table td {
            padding: 0.85rem 1rem;
            border-bottom: 1px solid var(--border-subtle);
            color: var(--text-primary);
            vertical-align: middle;
        }

        .file-table tr:last-child td {
            border-bottom: none;
        }

        .file-table tr:hover td {
            background-color: var(--bg-surface-hover);
        }

        /* Professional File Type Icon Box */
        .file-name-cell {
            display: flex;
            align-items: center;
            gap: 0.85rem;
            font-weight: 500;
            min-width: 0;
        }

        .file-icon-box {
            width: 38px;
            height: 38px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }

        .file-icon-box svg {
            width: 18px;
            height: 18px;
            stroke-width: 1.8;
            stroke: currentColor;
            fill: none;
            stroke-linecap: round;
            stroke-linejoin: round;
        }

        /* File Type Semantic Color Palette */
        .file-icon-box.file-video { background: rgba(168, 85, 247, 0.12); color: #c084fc; border-color: rgba(168, 85, 247, 0.25); }
        .file-icon-box.file-audio { background: rgba(20, 184, 166, 0.12); color: #2dd4bf; border-color: rgba(20, 184, 166, 0.25); }
        .file-icon-box.file-image { background: rgba(59, 130, 246, 0.12); color: #60a5fa; border-color: rgba(59, 130, 246, 0.25); }
        .file-icon-box.file-pdf { background: rgba(244, 63, 94, 0.12); color: #fb7185; border-color: rgba(244, 63, 94, 0.25); }
        .file-icon-box.file-text { background: rgba(148, 163, 184, 0.12); color: #cbd5e1; border-color: rgba(148, 163, 184, 0.2); }
        .file-icon-box.file-code { background: rgba(234, 179, 8, 0.12); color: #facc15; border-color: rgba(234, 179, 8, 0.25); }
        .file-icon-box.file-archive { background: rgba(139, 92, 246, 0.12); color: #a78bfa; border-color: rgba(139, 92, 246, 0.25); }
        .file-icon-box.file-exe { background: rgba(249, 115, 22, 0.12); color: #fb923c; border-color: rgba(249, 115, 22, 0.25); }
        .file-icon-box.file-doc { background: rgba(37, 99, 235, 0.12); color: #93c5fd; border-color: rgba(37, 99, 235, 0.25); }
        .file-icon-box.file-sheet { background: rgba(16, 185, 129, 0.12); color: #34d399; border-color: rgba(16, 185, 129, 0.25); }
        .file-icon-box.file-pres { background: rgba(249, 115, 22, 0.12); color: #fb923c; border-color: rgba(249, 115, 22, 0.25); }
        .file-icon-box.file-design { background: rgba(236, 72, 153, 0.12); color: #f472b6; border-color: rgba(236, 72, 153, 0.25); }
        .file-icon-box.file-generic { background: rgba(100, 116, 139, 0.12); color: #94a3b8; border-color: rgba(100, 116, 139, 0.2); }

        .file-title-group {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
            min-width: 0;
            flex: 1;
        }

        .file-name-text {
            font-size: 0.88rem;
            font-weight: 600;
            color: var(--text-primary);
            overflow-wrap: anywhere;
            word-break: break-word;
            line-height: 1.35;
        }

        .file-type-subtext {
            font-size: 0.76rem;
            color: var(--text-tertiary);
            line-height: 1.3;
        }

        .file-size-cell {
            font-family: var(--font-mono);
            font-size: 0.8rem;
            color: var(--text-secondary);
            white-space: nowrap;
        }

        .file-date-cell {
            font-size: 0.8rem;
            color: var(--text-tertiary);
            white-space: nowrap;
        }

        .empty-state {
            padding: 3rem 1.25rem;
            text-align: center;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 0.35rem;
        }

        .empty-state-title {
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--text-secondary);
        }

        .empty-state-subtitle {
            font-size: 0.8rem;
            color: var(--text-tertiary);
        }

        /* Unified Connect Device Modal */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(6px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 100;
            padding: 1rem;
        }

        .modal-dialog {
            background: var(--bg-surface);
            border: 1px solid var(--border-strong);
            border-radius: var(--radius-lg);
            padding: 1.5rem;
            max-width: min(100% - 1.5rem, 380px);
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1.15rem;
            box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.7);
        }

        .modal-header {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .modal-title {
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .modal-subtitle {
            font-size: 0.8rem;
            color: var(--text-secondary);
            text-align: center;
            line-height: 1.4;
        }

        .qr-card {
            background: #ffffff;
            padding: 0.85rem;
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            width: 210px;
            height: 210px;
            max-width: 100%;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }

        .qr-card svg, .qr-card img {
            width: 100%;
            height: 100%;
        }

        .modal-url-box {
            width: 100%;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 0.45rem 0.65rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
        }

        .modal-url-text {
            font-family: var(--font-mono);
            font-size: 0.8rem;
            color: var(--accent-text);
            word-break: break-all;
        }

        .modal-footer-note {
            font-size: 0.75rem;
            color: var(--text-tertiary);
            text-align: center;
        }

        /* Toast Notifications */
        .toast-container {
            position: fixed;
            bottom: 1.5rem;
            right: 1.5rem;
            left: 1.5rem;
            z-index: 90;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 0.5rem;
            pointer-events: none;
        }

        @media (min-width: 641px) {
            .toast-container { left: auto; align-items: flex-end; }
        }

        .toast {
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-strong);
            color: var(--text-primary);
            padding: 0.65rem 1rem;
            border-radius: var(--radius-sm);
            font-size: 0.82rem;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            gap: 0.5rem;
            animation: toastIn 0.2s ease-out;
            max-width: 100%;
        }

        @keyframes toastIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* -----------------------------------------------------------------
           MOBILE RESPONSIVE LAYOUT (< 640px)
        ----------------------------------------------------------------- */
        @media (max-width: 640px) {
            .workspace {
                padding: 1.25rem 0.85rem 3rem;
                gap: 1.75rem;
            }

            .desktop-text { display: none; }
            .mobile-text { display: inline; }

            .dropzone-container {
                padding: 2rem 1rem;
                min-height: 180px;
            }

            .btn {
                min-height: 40px;
                padding: 0.45rem 0.85rem;
            }

            .btn-sm {
                min-height: 36px;
                padding: 0.38rem 0.75rem;
            }

            .file-icon-box {
                width: 36px;
                height: 36px;
            }

            .file-icon-box svg {
                width: 17px;
                height: 17px;
            }

            /* Responsive Table-to-Card transformation */
            .file-table, .file-table tbody, .file-table tr, .file-table td {
                display: block;
                width: 100%;
            }

            .file-table thead {
                display: none;
            }

            .file-table tr {
                border-bottom: 1px solid var(--border-subtle);
                padding: 0.9rem 0.85rem;
                display: flex;
                flex-direction: column;
                gap: 0.65rem;
            }

            .file-table td {
                padding: 0;
                border: none;
            }

            .desktop-only-col {
                display: none !important;
            }

            .file-table td:last-child {
                width: 100%;
            }

            .file-table td:last-child .btn {
                width: 100%;
            }

            .nav-actions {
                width: 100%;
                justify-content: space-between;
            }

            .nav-actions .btn {
                flex: 1 1 auto;
            }

            .lan-pill-btn {
                width: 100%;
                justify-content: center;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            * { transition: none !important; animation: none !important; }
        }
    </style>
</head>
<body>

<nav class="app-nav" aria-label="Main Navigation">
    <div class="nav-inner">
        <a href="/" class="brand-group" aria-label="QuickShare Home">
            <svg class="brand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M7 17l9.2-9.2M17 17V7H7"/>
            </svg>
            <span class="brand-title">QuickShare</span>
            <span class="brand-badge">LAN</span>
        </a>

        <div class="nav-actions">
            <div class="lan-pill-btn" onclick="openConnectModal()" title="Connected to Local Network">
                <div class="status-dot"></div>
                <span class="lan-ip-text">{{ lan_ip }}:{{ port }}</span>
            </div>
            <button class="btn btn-sm btn-primary" onclick="openConnectModal()" aria-label="Connect another device">
                <svg class="svg-icon" viewBox="0 0 24 24">
                    <rect x="3" y="3" width="7" height="7"></rect>
                    <rect x="14" y="3" width="7" height="7"></rect>
                    <rect x="14" y="14" width="7" height="7"></rect>
                    <rect x="3" y="14" width="7" height="7"></rect>
                </svg>
                <span>Connect device</span>
            </button>
        </div>
    </div>
</nav>

<main class="workspace">
    <!-- Interrupted Upload Recovery Container -->
    <div id="resumeContainer" aria-live="polite"></div>

    <!-- Upload Section -->
    <section aria-labelledby="uploadSectionHeading" style="width: 100%;">
        <div class="section-header">
            <h2 class="section-title" id="uploadSectionHeading">Share Files</h2>
        </div>

        <div class="dropzone-container" id="dropzone" tabindex="0" role="button" aria-label="File drop area. Drop files to share or click to browse" onclick="document.getElementById('fileInput').click()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();document.getElementById('fileInput').click();}">
            <div class="dropzone-icon-box">
                <svg class="svg-icon" style="width: 20px; height: 20px;" viewBox="0 0 24 24">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
            </div>
            <div class="dropzone-text-group">
                <div class="dropzone-title">
                    <span class="desktop-text">Drop files here to share</span>
                    <span class="mobile-text">Select files to share</span>
                </div>
                <div class="dropzone-subtitle">Chunked · Resumable · End-to-end verified</div>
            </div>
            <button type="button" class="btn btn-primary btn-sm" onclick="event.stopPropagation(); document.getElementById('fileInput').click()">Browse files</button>
            <input type="file" id="fileInput" class="file-input" multiple onchange="handleFileSelection(event)">
        </div>

        <div class="transfer-list" id="uploadQueue" style="margin-top: 1rem;" aria-live="polite"></div>
    </section>

    <!-- Available Files Section with File Category Filtering -->
    <section aria-labelledby="filesSectionHeading" style="width: 100%;">
        <div class="section-header">
            <h2 class="section-title" id="filesSectionHeading">Available Files (<span id="fileCount">{{ files|length }}</span>)</h2>
        </div>

        <div class="files-container">
            {% if files %}
            <div class="files-toolbar">
                <div class="toolbar-top-row">
                    <div class="search-box">
                        <svg class="search-icon svg-icon" viewBox="0 0 24 24">
                            <circle cx="11" cy="11" r="8"></circle>
                            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                        <input type="search" id="fileSearch" class="search-input" placeholder="Search files..." aria-label="Search available files" oninput="applyFilters()">
                    </div>
                </div>

                <div class="filter-pills-bar" role="tablist" aria-label="File Category Filters">
                    <button class="filter-pill active" data-category="all" onclick="setCategoryFilter('all', this)">All</button>
                    <button class="filter-pill" data-category="images" onclick="setCategoryFilter('images', this)">Images</button>
                    <button class="filter-pill" data-category="videos" onclick="setCategoryFilter('videos', this)">Videos</button>
                    <button class="filter-pill" data-category="audio" onclick="setCategoryFilter('audio', this)">Audio</button>
                    <button class="filter-pill" data-category="documents" onclick="setCategoryFilter('documents', this)">Documents</button>
                    <button class="filter-pill" data-category="archives" onclick="setCategoryFilter('archives', this)">Archives</button>
                    <button class="filter-pill" data-category="code" onclick="setCategoryFilter('code', this)">Code</button>
                    <button class="filter-pill" data-category="applications" onclick="setCategoryFilter('applications', this)">Applications</button>
                    <button class="filter-pill" data-category="other" onclick="setCategoryFilter('other', this)">Other</button>
                </div>
            </div>

            <div id="noFilterMatches" class="empty-state" style="display: none;">
                <div class="empty-state-title">No matching files</div>
                <div class="empty-state-subtitle">Try another search query or file category.</div>
            </div>

            <table class="file-table" id="fileTable">
                <thead>
                    <tr>
                        <th scope="col">Name</th>
                        <th scope="col" class="desktop-only-col" style="width: 100px;">Size</th>
                        <th scope="col" class="desktop-only-col" style="width: 120px;">Added</th>
                        <th scope="col" style="width: 120px; text-align: right;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {% for f in files %}
                    <tr class="file-row" data-filename="{{ f.name|lower }}" data-type="{{ f.type_info.label|lower }}" data-category="{{ f.type_info.category }}">
                        <td>
                            <div class="file-name-cell">
                                <div class="file-icon-box {{ f.type_info.badge_class }}" aria-hidden="true">
                                    {% if f.type_info.icon == 'video' %}
                                        <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                                    {% elif f.type_info.icon == 'audio' %}
                                        <svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>
                                    {% elif f.type_info.icon == 'image' %}
                                        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>
                                    {% elif f.type_info.icon == 'pdf' %}
                                        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="9" y1="13" x2="15" y2="13"></line><line x1="9" y1="17" x2="13" y2="17"></line></svg>
                                    {% elif f.type_info.icon == 'text' %}
                                        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><line x1="10" y1="9" x2="8" y2="9"></line></svg>
                                    {% elif f.type_info.icon == 'code' %}
                                        <svg viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>
                                    {% elif f.type_info.icon == 'archive' %}
                                        <svg viewBox="0 0 24 24"><polyline points="21 8 21 21 3 21 3 8"></polyline><rect x="1" y="3" width="22" height="5"></rect><line x1="10" y1="12" x2="14" y2="12"></line></svg>
                                    {% elif f.type_info.icon == 'exe' %}
                                        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"></rect><path d="M7 8l4 4-4 4"></path><line x1="13" y1="16" x2="17" y2="16"></line></svg>
                                    {% elif f.type_info.icon == 'doc' %}
                                        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
                                    {% elif f.type_info.icon == 'sheet' %}
                                        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="3" y1="9" x2="21" y2="9"></line><line x1="3" y1="15" x2="21" y2="15"></line><line x1="9" y1="3" x2="9" y2="21"></line><line x1="15" y1="3" x2="15" y2="21"></line></svg>
                                    {% elif f.type_info.icon == 'pres' %}
                                        <svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                                    {% elif f.type_info.icon == 'design' %}
                                        <svg viewBox="0 0 24 24"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>
                                    {% else %}
                                        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                                    {% endif %}
                                </div>
                                <div class="file-title-group">
                                    <span class="file-name-text" title="{{ f.name }}">{{ f.name }}</span>
                                    <span class="file-type-subtext">{{ f.type_info.label }} · {{ f.size_str }} · {{ f.mtime_str }}</span>
                                </div>
                            </div>
                        </td>
                        <td class="file-size-cell desktop-only-col">{{ f.size_str }}</td>
                        <td class="file-date-cell desktop-only-col">{{ f.mtime_str }}</td>
                        <td style="text-align: right;">
                            <a href="/download/{{ f.name }}" class="btn btn-sm" download aria-label="Download {{ f.name }}">
                                <svg class="svg-icon" viewBox="0 0 24 24">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
                                </svg>
                                <span>Download</span>
                            </a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty-state">
                <div class="empty-state-title">No files available yet</div>
                <div class="empty-state-subtitle">Files shared from your devices will appear here for download.</div>
            </div>
            {% endif %}
        </div>
    </section>
</main>

<!-- Unified Connect Device Modal -->
<div class="modal-overlay" id="connectModal" onclick="closeConnectModal(event)">
    <div class="modal-dialog" onclick="event.stopPropagation()">
        <div class="modal-header">
            <span class="modal-title">Connect another device</span>
            <button class="btn btn-icon-only" onclick="closeConnectModal()" aria-label="Close dialog">
                <svg class="svg-icon" viewBox="0 0 24 24">
                    <line x1="18" y1="6" x2="6" y2="18"></line>
                    <line x1="6" y1="6" x2="18" y2="18"></line>
                </svg>
            </button>
        </div>

        <div class="modal-subtitle">
            Scan this QR code with your phone camera or open the address in any browser
        </div>

        <div class="qr-card">
            {% if qr_svg %}
                {{ qr_svg|safe }}
            {% else %}
                <img src="/qr" alt="Scan QR code to connect" style="width:100%; height:100%;">
            {% endif %}
        </div>

        <div class="modal-url-box">
            <span class="modal-url-text" id="lanUrlText">{{ lan_url }}</span>
            <button class="btn btn-sm" id="copyUrlBtn" onclick="copyLanUrl()">
                <svg class="svg-icon" id="copyIcon" viewBox="0 0 24 24">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                </svg>
                <span id="copyBtnText">Copy address</span>
            </button>
        </div>

        <div class="modal-footer-note">
            Devices must be connected to the same local network.
        </div>
    </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
// ---------------------------------------------------------------------------
// Client Configuration
// ---------------------------------------------------------------------------
const CHUNK_SIZE = 5 * 1024 * 1024; // 5 MB optimal chunk size
const UPLOAD_CONCURRENCY = 3;       // 3 concurrent chunk workers per file
const MAX_CHUNK_RETRIES = 5;        // Max retry attempts per chunk with exponential backoff
const STORAGE_KEY = "quickshare_active_uploads";

let activeUploaders = new Map();
let currentCategory = 'all';

// Helper: Format bytes
function formatBytes(bytes) {
    if (bytes === 0 || !bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Helper: Format seconds to clean ETA string
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

// Helper: Toast Notifications
function showToast(message) {
    const container = document.getElementById("toastContainer");
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.2s ease';
        setTimeout(() => toast.remove(), 200);
    }, 2800);
}

// Helper: Copy LAN URL
function copyLanUrl() {
    const url = document.getElementById("lanUrlText").textContent;
    navigator.clipboard.writeText(url).then(() => {
        const copyText = document.getElementById("copyBtnText");
        if (copyText) copyText.textContent = "Address copied";
        showToast("Address copied to clipboard");
        if (copyText) setTimeout(() => { copyText.textContent = "Copy address"; }, 2000);
    }).catch(() => {
        showToast("Address: " + url);
    });
}

// Unified Connect Device Modal controls
function openConnectModal() {
    document.getElementById("connectModal").style.display = "flex";
}

function closeConnectModal(event) {
    document.getElementById("connectModal").style.display = "none";
}

// Keyboard escape to close modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeConnectModal();
    }
});

// ---------------------------------------------------------------------------
// File Category Filter & Search Logic
// ---------------------------------------------------------------------------
function setCategoryFilter(category, buttonEl) {
    currentCategory = category;
    document.querySelectorAll('.filter-pill').forEach(btn => btn.classList.remove('active'));
    if (buttonEl) buttonEl.classList.add('active');
    applyFilters();
}

function applyFilters() {
    const query = document.getElementById('fileSearch') ? document.getElementById('fileSearch').value.toLowerCase().trim() : '';
    const rows = document.querySelectorAll('.file-row');
    let visible = 0;

    rows.forEach(row => {
        const fname = row.getAttribute('data-filename') || '';
        const ftype = row.getAttribute('data-type') || '';
        const fcat = row.getAttribute('data-category') || 'other';

        const matchesCategory = (currentCategory === 'all' || fcat === currentCategory);
        const matchesSearch = (!query || fname.includes(query) || ftype.includes(query));

        if (matchesCategory && matchesSearch) {
            row.style.display = '';
            visible++;
        } else {
            row.style.display = 'none';
        }
    });

    const fileCount = document.getElementById('fileCount');
    if (fileCount) fileCount.textContent = visible;

    const noMatches = document.getElementById('noFilterMatches');
    const fileTable = document.getElementById('fileTable');
    if (noMatches) {
        if (visible === 0 && rows.length > 0) {
            noMatches.style.display = 'flex';
            if (fileTable) fileTable.style.display = 'none';
        } else {
            noMatches.style.display = 'none';
            if (fileTable) fileTable.style.display = '';
        }
    }
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

// Check for interrupted uploads on page load
function checkSavedSessions() {
    const sessions = getSavedUploads();
    const resumeContainer = document.getElementById("resumeContainer");
    resumeContainer.innerHTML = "";

    const sessionKeys = Object.keys(sessions);
    if (sessionKeys.length === 0) return;

    sessionKeys.forEach(uploadId => {
        const session = sessions[uploadId];
        const banner = document.createElement("div");
        banner.className = "session-resume-alert";
        banner.id = `resume-alert-${uploadId}`;
        banner.innerHTML = `
            <div class="session-resume-text">
                <strong>Interrupted transfer:</strong> ${escapeHtml(session.filename)} (${formatBytes(session.total_size)})<br>
                <span style="color: var(--text-secondary); font-size: 0.8rem;">Select the original file to resume without re-uploading completed chunks.</span>
            </div>
            <div class="session-resume-actions">
                <label class="btn btn-sm btn-primary" style="cursor: pointer;">
                    Resume
                    <input type="file" style="display:none;" onchange="resumeSessionFile(event, '${uploadId}')">
                </label>
                <button class="btn btn-sm" onclick="dismissSavedSession('${uploadId}')">Dismiss</button>
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

    // Multi-factor identity check: filename, total size, and modification timestamp
    if (file.name !== session.filename || file.size !== session.total_size || (session.last_modified && file.lastModified !== session.last_modified)) {
        alert("The selected file does not match the interrupted upload ('" + session.filename + "'). Please select the exact file.");
        return;
    }

    dismissSavedSession(uploadId);
    startChunkedUpload(file, uploadId);
}

// ---------------------------------------------------------------------------
// Unified Chunked Uploader Implementation (High Throughput & Optimized UI)
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
        
        // Strict concurrency control: ONE uploader = ONE active worker group
        this.uploadGeneration = 0;
        this.isLoopRunning = false;
        this.isReconciling = false;
        
        // Progress & Smoothed Speed tracking (Exponential Moving Average)
        this.uploadedBytes = 0;
        this.lastSpeedCheck = Date.now();
        this.bytesSinceLastCheck = 0;
        this.currentSpeed = 0;
        this.uiUpdatePending = false;
        
        // Abort controllers for active chunk requests
        this.abortControllers = new Map();
        
        // Unique DOM card ID
        this.elementId = 'upload-' + (this.uploadId || 'temp-' + Math.random().toString(36).substr(2, 9));
    }

    renderCard() {
        const queue = document.getElementById("uploadQueue");
        let card = document.getElementById(this.elementId);
        if (!card) {
            card = document.createElement("div");
            card.className = "transfer-card";
            card.id = this.elementId;
            queue.prepend(card);
        }

        card.innerHTML = `
            <div class="transfer-header">
                <div class="transfer-title-group">
                    <div class="transfer-filename" title="${escapeHtml(this.filename)}">${escapeHtml(this.filename)}</div>
                    <div class="transfer-meta">${formatBytes(this.totalSize)} · <span id="${this.elementId}-chunks">0/${this.totalChunks} chunks</span></div>
                </div>
                <div class="transfer-actions">
                    <span class="status-badge badge-uploading" id="${this.elementId}-badge">UPLOADING</span>
                    <button class="btn btn-sm" id="${this.elementId}-pause-btn" onclick="togglePauseUpload('${this.elementId}')">Pause</button>
                    <button class="btn btn-sm btn-danger" id="${this.elementId}-cancel-btn" onclick="cancelUpload('${this.elementId}')">Cancel</button>
                </div>
            </div>
            <div class="progress-track" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100" id="${this.elementId}-progress-container">
                <div class="progress-fill" id="${this.elementId}-fill" style="width: 0%"></div>
            </div>
            <div class="transfer-footer">
                <span id="${this.elementId}-progress-text">0% · 0 B / ${formatBytes(this.totalSize)}</span>
                <span id="${this.elementId}-speed-text">-- MB/s · ETA: --</span>
            </div>
        `;
    }

    requestUIUpdate() {
        if (this.uiUpdatePending) return;
        this.uiUpdatePending = true;
        requestAnimationFrame(() => {
            this.uiUpdatePending = false;
            this.updateUI();
        });
    }

    updateUI() {
        const card = document.getElementById(this.elementId);
        if (!card) return;

        const badge = document.getElementById(`${this.elementId}-badge`);
        const fill = document.getElementById(`${this.elementId}-fill`);
        const progressContainer = document.getElementById(`${this.elementId}-progress-container`);
        const progressText = document.getElementById(`${this.elementId}-progress-text`);
        const speedText = document.getElementById(`${this.elementId}-speed-text`);
        const chunksText = document.getElementById(`${this.elementId}-chunks`);
        const pauseBtn = document.getElementById(`${this.elementId}-pause-btn`);
        const cancelBtn = document.getElementById(`${this.elementId}-cancel-btn`);

        if (chunksText) {
            chunksText.textContent = `${this.receivedChunks.size}/${this.totalChunks} chunks`;
        }

        // Fast O(1) progress calculation
        let percent = 0;
        if (this.totalSize > 0) {
            let confirmedBytes = 0;
            if (this.receivedChunks.size === this.totalChunks) {
                confirmedBytes = this.totalSize;
            } else {
                for (let idx of this.receivedChunks) {
                    const start = idx * this.chunkSize;
                    const end = Math.min(start + this.chunkSize, this.totalSize);
                    confirmedBytes += (end - start);
                }
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

        if (progressContainer) {
            progressContainer.setAttribute('aria-valuenow', percent);
        }

        if (progressText) {
            progressText.textContent = `${percent}% · ${formatBytes(this.uploadedBytes)} / ${formatBytes(this.totalSize)}`;
        }

        // Update ETA & Speed text
        if (speedText) {
            if (this.status === 'uploading') {
                const remainingBytes = Math.max(0, this.totalSize - this.uploadedBytes);
                const etaSeconds = this.currentSpeed > 0 ? (remainingBytes / this.currentSpeed) : 0;
                speedText.textContent = `${formatBytes(this.currentSpeed)}/s · ${formatTime(etaSeconds)} remaining`;
            } else if (this.status === 'assembling') {
                speedText.textContent = `Verifying integrity and finalizing...`;
            } else if (this.status === 'completed') {
                speedText.textContent = `Completed and verified`;
            } else if (this.status === 'cancelled') {
                speedText.textContent = `Transfer cancelled`;
            } else if (this.status === 'paused') {
                speedText.textContent = `Transfer paused`;
            } else if (this.status === 'error') {
                speedText.textContent = `${this.errorMessage || 'Transfer failed'}`;
            }
        }

        // Update Status Badges & Buttons
        if (badge) {
            badge.className = `status-badge badge-${this.status}`;
            badge.textContent = this.status.toUpperCase();
        }

        if (pauseBtn) {
            if (this.status === 'completed' || this.status === 'cancelled' || this.status === 'assembling') {
                pauseBtn.style.display = 'none';
            } else if (this.status === 'error') {
                pauseBtn.style.display = 'inline-block';
                pauseBtn.textContent = 'Retry';
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
            // Step 1: Initialize or Reconnect upload session
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
                    throw new Error(errData.error || "Upload session expired or not found on server");
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

            // Save session to localStorage
            saveUploadSession(this.uploadId, {
                upload_id: this.uploadId,
                filename: this.filename,
                total_size: this.totalSize,
                chunk_size: this.chunkSize,
                total_chunks: this.totalChunks,
                last_modified: this.lastModified
            });

            this.updateUI();

            // Step 2: Upload missing chunks concurrently and await loop completion
            await this.uploadChunksLoop();

            if (this.status === 'cancelled' || this.status === 'paused') return;

            // Step 3: Complete upload
            if (this.receivedChunks.size >= this.totalChunks) {
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
                showToast("File uploaded successfully");

                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            }

        } catch (err) {
            if (this.status === 'cancelled' || this.status === 'paused') return;
            console.error("Upload error:", err);
            this.status = 'error';
            this.errorMessage = err.message || "Network error occurred";
            this.updateUI();
        }
    }

    /**
     * UNIFIED RECOVERY PIPELINE:
     * Authoritative single path for network reconnect, visibility change, and manual user resume.
     */
    async reconcileAndResume(isUserResume = false) {
        if (!this.uploadId || this.isReconciling) return;
        if (this.status === 'completed' || this.status === 'cancelled' || this.status === 'assembling') return;
        
        // Strict pause preservation: Automatic background events will NEVER unpause a manually paused upload!
        if (this.status === 'paused' && !isUserResume) return;

        this.isReconciling = true;
        try {
            const statusRes = await fetch(`/upload/status/${this.uploadId}`);
            if (!statusRes.ok) {
                const errData = await statusRes.json().catch(() => ({}));
                this.status = 'error';
                this.errorMessage = errData.error || "Upload session expired or not found on server";
                removeUploadSession(this.uploadId);
                this.updateUI();
                return;
            }

            const statusData = await statusRes.json();
            if (!statusData.success) {
                this.status = 'error';
                this.errorMessage = statusData.error || "Upload session not found";
                removeUploadSession(this.uploadId);
                this.updateUI();
                return;
            }

            // Treat server as authoritative
            this.receivedChunks = new Set(statusData.received_chunks);
            this.chunkSize = statusData.chunk_size;
            this.totalChunks = statusData.total_chunks;

            if (statusData.status === 'assembling') {
                this.status = 'assembling';
                this.updateUI();
                return;
            }

            if (statusData.status === 'completed') {
                this.status = 'completed';
                removeUploadSession(this.uploadId);
                this.updateUI();
                setTimeout(() => location.reload(), 1000);
                return;
            }

            // Check if all chunks already uploaded
            if (this.receivedChunks.size >= this.totalChunks) {
                this.status = 'assembling';
                this.updateUI();
                const completeRes = await fetch('/upload/complete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ upload_id: this.uploadId })
                });
                const completeData = await completeRes.json();
                if (completeData.success) {
                    this.status = 'completed';
                    removeUploadSession(this.uploadId);
                    this.updateUI();
                    setTimeout(() => location.reload(), 1000);
                } else {
                    this.status = 'error';
                    this.errorMessage = completeData.error || "Assembly failed";
                    this.updateUI();
                }
                return;
            }

            // Upload missing chunks
            if (isUserResume || this.status !== 'paused') {
                this.status = 'uploading';
                this.errorMessage = '';
                this.lastSpeedCheck = Date.now();
                this.bytesSinceLastCheck = 0;
                this.updateUI();
                
                await this.uploadChunksLoop();

                if (this.status === 'uploading' && this.receivedChunks.size >= this.totalChunks) {
                    this.status = 'assembling';
                    this.updateUI();
                    const completeRes = await fetch('/upload/complete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ upload_id: this.uploadId })
                    });
                    const completeData = await completeRes.json();
                    if (completeData.success) {
                        this.status = 'completed';
                        removeUploadSession(this.uploadId);
                        this.updateUI();
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        this.status = 'error';
                        this.errorMessage = completeData.error || "Assembly failed";
                        this.updateUI();
                    }
                }
            } else {
                this.updateUI();
            }

        } catch (err) {
            console.warn("Reconciliation network error:", err);
            if (isUserResume) {
                this.status = 'error';
                this.errorMessage = "Network reconnection failed. Please retry.";
                this.updateUI();
            }
        } finally {
            this.isReconciling = false;
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

            // Smoothed speed calculation timer (every 400ms)
            const speedInterval = setInterval(() => {
                if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) {
                    clearInterval(speedInterval);
                    return;
                }
                const now = Date.now();
                const elapsed = (now - this.lastSpeedCheck) / 1000;
                if (elapsed > 0.4) {
                    const instantSpeed = this.bytesSinceLastCheck / elapsed;
                    this.currentSpeed = this.currentSpeed === 0 ? Math.round(instantSpeed) : Math.round(0.7 * instantSpeed + 0.3 * this.currentSpeed);
                    this.bytesSinceLastCheck = 0;
                    this.lastSpeedCheck = now;
                    this.requestUIUpdate();
                }
            }, 400);

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
                                this.requestUIUpdate();
                            } catch (err) {
                                if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) break;
                                retries++;
                                console.warn(`Retry ${retries}/${MAX_CHUNK_RETRIES} for chunk ${chunkIndex}:`, err);
                                await new Promise(r => setTimeout(r, Math.min(600 * Math.pow(1.5, retries), 5000)));
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

        for (let controller of this.abortControllers.values()) {
            controller.abort();
        }
        this.abortControllers.clear();
        this.updateUI();
        showToast("Transfer paused");
    }

    resume() {
        showToast("Resuming transfer");
        return this.reconcileAndResume(true);
    }

    async cancel() {
        this.status = 'cancelled';
        this.uploadGeneration++;
        this.isLoopRunning = false;

        for (let controller of this.abortControllers.values()) {
            controller.abort();
        }
        this.abortControllers.clear();

        if (this.uploadId) {
            try {
                await fetch(`/upload/cancel/${this.uploadId}`, { method: 'POST' });
            } catch (e) {
                console.warn("Failed to notify server of cancellation:", e);
            }
            removeUploadSession(this.uploadId);
        }

        this.updateUI();
        showToast("Transfer cancelled");
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
        if (uploader.status === 'paused' || uploader.status === 'error') {
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

// Page visibility and connection recovery
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        activeUploaders.forEach(uploader => {
            uploader.reconcileAndResume(false);
        });
    }
});

window.addEventListener('online', () => {
    console.info("Network online detected: reconciling active uploads");
    showToast("Network restored");
    activeUploaders.forEach(uploader => {
        uploader.reconcileAndResume(false);
    });
});

window.addEventListener('offline', () => {
    showToast("Network connection lost");
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
    """Renders the main QuickShare page listing completed files in uploads/ and LAN details."""
    file_list = []
    if os.path.exists(UPLOAD_DIR):
        try:
            for fname in os.listdir(UPLOAD_DIR):
                full_path = os.path.join(UPLOAD_DIR, fname)
                if os.path.isfile(full_path) and not fname.startswith('.') and is_safe_path(UPLOAD_DIR, full_path):
                    stat = os.stat(full_path)
                    type_info = get_file_type_info(fname)
                    file_list.append({
                        "name": fname,
                        "size": stat.st_size,
                        "size_str": format_bytes(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
                        "type_info": type_info
                    })
            file_list.sort(key=lambda x: x["mtime"], reverse=True)
        except Exception as e:
            logger.error(f"Error listing uploads directory: {e}")
            
    lan_ip = get_lan_ip()
    lan_url = f"http://{lan_ip}:{PORT}"
    qr_svg = generate_qr_svg(lan_url)

    return render_template_string(
        UPLOAD_HTML,
        files=file_list,
        lan_ip=lan_ip,
        port=PORT,
        lan_url=lan_url,
        qr_svg=qr_svg
    )


@app.route('/qr')
def qr_code_endpoint():
    """Returns SVG QR code for the current server LAN access URL."""
    lan_ip = get_lan_ip()
    lan_url = f"http://{lan_ip}:{PORT}"
    svg = generate_qr_svg(lan_url)
    if svg:
        return Response(svg, mimetype='image/svg+xml')
    else:
        return jsonify({"success": False, "error": "QR generator not available"}), 404


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
        "received_chunks": set(),
        "file_hash": file_hash,
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "uploading"
    }

    with upload_lock_manager.acquire(upload_id):
        if not save_metadata(upload_id, metadata, write_disk=True):
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
    """Receives and securely stores a single chunk for an upload session with lock-free disk I/O."""
    upload_id = request.form.get("upload_id")
    chunk_index_raw = request.form.get("chunk_index")
    total_chunks_raw = request.form.get("total_chunks")
    chunk_file = request.files.get("chunk")

    if not upload_id or not is_valid_uuid(upload_id):
        return jsonify({"success": False, "error": "Invalid or missing upload_id"}), 400

    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir or not os.path.exists(cache_dir):
        return jsonify({"success": False, "error": "Upload session not found or expired"}), 404

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

    # Lock-free binary disk write to temporary chunk path
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

        # Micro-second lock to update received set in memory
        with upload_lock_manager.acquire(upload_id):
            metadata["received_chunks"].add(chunk_index)
            metadata["updated_at"] = time.time()
            save_metadata(upload_id, metadata, write_disk=False)

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
        save_metadata(upload_id, metadata, write_disk=True)

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
    """Three-phase non-blocking assembly: validates state, streams chunks with 1MB buffer, and atomically finalizes."""
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
        save_metadata(upload_id, metadata, write_disk=True)
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
                        buffer = infile.read(STREAM_BUFFER_SIZE)
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
            upload_lock_manager.remove_cached_meta(upload_id)
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
            save_metadata(upload_id, latest_meta, write_disk=True)
            return jsonify({"success": False, "error": assembly_error}), 400

        # Atomic collision-safe final filename generation
        with upload_lock_manager.filename_lock():
            final_filename = get_unique_filename(UPLOAD_DIR, latest_meta["safe_filename"])
            final_dest = os.path.join(UPLOAD_DIR, final_filename)
            shutil.move(assembled_tmp, final_dest)

        latest_meta["status"] = "completed"
        latest_meta["safe_filename"] = final_filename
        latest_meta["updated_at"] = time.time()
        
        upload_lock_manager.remove_cached_meta(upload_id)
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
        upload_lock_manager.remove_cached_meta(upload_id)
        if os.path.exists(cache_dir):
            try:
                meta = load_metadata(upload_id)
                if meta:
                    meta["status"] = "cancelled"
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta, write_disk=False)

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
    """
    Securely serves completed files strictly from uploads/.
    Supports HTTP Range requests (RFC 7233 / 9110), streaming, and safe path resolution.
    Never serves from cache/ or temporary files.
    """
    safe_name = os.path.basename(filename)
    target_path = os.path.join(UPLOAD_DIR, safe_name)
    
    if not is_safe_path(UPLOAD_DIR, target_path):
        abort(403)
        
    if not os.path.exists(target_path) or not os.path.isfile(target_path):
        abort(404)
        
    return send_from_directory(UPLOAD_DIR, safe_name, as_attachment=True, conditional=True)


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    lan_ip = get_lan_ip()
    logger.info(f"Starting QuickShare LAN File Transfer Server on http://{lan_ip}:{PORT} (Listening on {HOST}:{PORT}, debug={DEBUG})")
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)