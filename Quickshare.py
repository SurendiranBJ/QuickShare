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
import zipfile
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
DEFAULT_CHUNK_SIZE = int(os.getenv("DEFAULT_CHUNK_SIZE", 8 * 1024 * 1024))  # 8 MB default for high LAN throughput
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
    """
    Generates a clean, self-contained, 100% valid SVG QR code string.
    Includes a crisp white background, black modules with border/quiet zone,
    and responsive viewBox (width="100%" height="100%").
    Works completely offline with zero external dependencies.
    """
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=3
        )
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        
        path_segments = []
        for r_idx, row in enumerate(matrix):
            for c_idx, val in enumerate(row):
                if val:
                    path_segments.append(f"M{c_idx},{r_idx}h1v1h-1z")
        
        path_data = "".join(path_segments)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="100%" height="100%" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
            f'<path d="{path_data}" fill="#000000"/>'
            f'</svg>'
        )
        return svg
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

def get_file_type_info(filename, is_folder=False):
    """
    Unified Single Source of Truth for File Type Detection:
    Returns (category, label, badge_class, icon) based on filename extension or folder flag.
    Categories: 'folders', 'images', 'videos', 'audio', 'documents', 'archives', 'code', 'applications', 'other'.
    Handles case-insensitivity, multiple dots, and files without extensions.
    """
    if is_folder:
        return {
            "category": "folders",
            "label": "Folder",
            "badge_class": "file-folder",
            "icon": "folder"
        }

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
        "doc": ("documents", "Word Document", "file-doc", "doc"),
        "docx": ("documents", "Word Document", "file-doc", "doc"),
        "xls": ("documents", "Excel Spreadsheet", "file-sheet", "sheet"),
        "xlsx": ("documents", "Excel Spreadsheet", "file-sheet", "sheet"),
        "ppt": ("documents", "PowerPoint Presentation", "file-pres", "pres"),
        "pptx": ("documents", "PowerPoint Presentation", "file-pres", "pres"),
        "txt": ("documents", "Plain Text", "file-text", "text"),
        "md": ("documents", "Markdown Document", "file-text", "text"),
        "rtf": ("documents", "Rich Text", "file-text", "text"),
        "csv": ("documents", "CSV Spreadsheet", "file-sheet", "sheet"),
        
        # Archives
        "zip": ("archives", "ZIP Archive", "file-archive", "archive"),
        "tar": ("archives", "TAR Archive", "file-archive", "archive"),
        "gz": ("archives", "GZ Archive", "file-archive", "archive"),
        "tar.gz": ("archives", "TAR.GZ Archive", "file-archive", "archive"),
        "bz2": ("archives", "BZ2 Archive", "file-archive", "archive"),
        "tar.bz2": ("archives", "TAR.BZ2 Archive", "file-archive", "archive"),
        "xz": ("archives", "XZ Archive", "file-archive", "archive"),
        "tar.xz": ("archives", "TAR.XZ Archive", "file-archive", "archive"),
        "7z": ("archives", "7Z Archive", "file-archive", "archive"),
        "rar": ("archives", "RAR Archive", "file-archive", "archive"),
        "iso": ("archives", "ISO Disk Image", "file-archive", "archive"),
        
        # Code & Scripts
        "py": ("code", "Python Script", "file-code", "code"),
        "js": ("code", "JavaScript File", "file-code", "code"),
        "ts": ("code", "TypeScript File", "file-code", "code"),
        "html": ("code", "HTML Document", "file-code", "code"),
        "htm": ("code", "HTML Document", "file-code", "code"),
        "css": ("code", "CSS Stylesheet", "file-code", "code"),
        "scss": ("code", "Sass Stylesheet", "file-code", "code"),
        "json": ("code", "JSON Data", "file-code", "code"),
        "c": ("code", "C Source", "file-code", "code"),
        "cpp": ("code", "C++ Source", "file-code", "code"),
        "h": ("code", "C Header", "file-code", "code"),
        "hpp": ("code", "C++ Header", "file-code", "code"),
        "cs": ("code", "C# Source", "file-code", "code"),
        "java": ("code", "Java Source", "file-code", "code"),
        "go": ("code", "Go Source", "file-code", "code"),
        "rs": ("code", "Rust Source", "file-code", "code"),
        "php": ("code", "PHP Script", "file-code", "code"),
        "sh": ("code", "Shell Script", "file-code", "code"),
        "bash": ("code", "Bash Script", "file-code", "code"),
        "ps1": ("code", "PowerShell Script", "file-code", "code"),
        "sql": ("code", "SQL Database Script", "file-code", "code"),
        "xml": ("code", "XML File", "file-code", "code"),
        "yaml": ("code", "YAML File", "file-code", "code"),
        "yml": ("code", "YAML File", "file-code", "code"),
        
        # Applications / Executables
        "exe": ("applications", "Application · EXE", "file-exe", "exe"),
        "msi": ("applications", "Windows Installer", "file-exe", "exe"),
        "apk": ("applications", "Android Package", "file-exe", "exe"),
        "dmg": ("applications", "macOS Disk Image", "file-exe", "exe"),
        "pkg": ("applications", "macOS Package", "file-exe", "exe"),
        "deb": ("applications", "Debian Package", "file-exe", "exe"),
        "rpm": ("applications", "RedHat Package", "file-exe", "exe"),
        "appimage": ("applications", "AppImage", "file-exe", "exe"),
        
        # Other specialized
        "psd": ("other", "Photoshop Document", "file-design", "design"),
        "ai": ("other", "Illustrator Artwork", "file-design", "design"),
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

def sanitize_relative_path(rel_path):
    """
    Sanitizes a relative path inside a folder, preventing path traversal and normalization escapes.
    Example: 'src/utils/config.py' -> 'src/utils/config.py'.
    Rejects empty segments, null bytes, '.', '..', and leading/trailing slashes.
    """
    if not rel_path:
        return ""
    clean = rel_path.replace('\\', '/').strip('/')
    parts = [p for p in clean.split('/') if p and p not in ('.', '..')]
    safe_parts = []
    for p in parts:
        sp = secure_filename(p)
        if sp:
            safe_parts.append(sp)
    return "/".join(safe_parts)

def sanitize_folder_name(name):
    """Sanitizes folder name safely."""
    if not name:
        return f"folder_{uuid.uuid4().hex[:8]}"
    clean = os.path.basename(name).strip()
    safe = secure_filename(clean)
    if not safe:
        safe = f"folder_{uuid.uuid4().hex[:8]}"
    return safe

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

def get_unique_folder_name(destination_dir, folder_name):
    """Generate a non-colliding folder name if a folder with the same name already exists."""
    safe = sanitize_folder_name(folder_name)
    target = os.path.join(destination_dir, safe)
    if not os.path.exists(target):
        return safe
    counter = 1
    while True:
        candidate = f"{safe} ({counter})"
        if not os.path.exists(os.path.join(destination_dir, candidate)):
            return candidate
        counter += 1

def get_folder_stats(folder_path):
    """Calculates total size (bytes) and file count of a directory recursively."""
    total_size = 0
    file_count = 0
    try:
        for root, dirs, files in os.walk(folder_path):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.isfile(fp) and not f.startswith('.'):
                    total_size += os.path.getsize(fp)
                    file_count += 1
    except Exception:
        pass
    return total_size, file_count

def get_upload_cache_dir(upload_id):
    """Get absolute path to an upload's cache directory with strict path containment check."""
    if not is_valid_uuid(upload_id):
        return None
    cache_path = os.path.join(CACHE_DIR, upload_id)
    if not is_safe_path(CACHE_DIR, cache_path):
        return None
    return cache_path

def get_folder_cache_dir(folder_id):
    """Get absolute path to a folder upload's cache directory with path containment check."""
    if not is_valid_uuid(folder_id):
        return None
    cache_path = os.path.join(CACHE_DIR, f"folder_{folder_id}")
    if not is_safe_path(CACHE_DIR, cache_path):
        return None
    return cache_path

def get_metadata_path(upload_id):
    """Get path to metadata.json for an upload."""
    cache_dir = get_upload_cache_dir(upload_id)
    if not cache_dir:
        return None
    return os.path.join(cache_dir, "metadata.json")

def get_folder_metadata_path(folder_id):
    """Get path to metadata.json for a folder upload."""
    cache_dir = get_folder_cache_dir(folder_id)
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
            # Normalize sets for in-memory use
            data["received_chunks"] = set(data.get("received_chunks", []))
            # in_flight_chunks tracks reservations made during Phase A
            data["in_flight_chunks"] = set(data.get("in_flight_chunks", []))
            upload_lock_manager.set_cached_meta(upload_id, data)
            return data
    except Exception as e:
        logger.error(f"Error reading metadata for upload_id={upload_id}: {e}")
        return None

def load_folder_metadata(folder_id):
    """Safely load metadata.json for a folder upload session."""
    cached = upload_lock_manager.get_cached_meta(f"folder_{folder_id}")
    if cached is not None:
        return cached

    meta_path = get_folder_metadata_path(folder_id)
    if not meta_path or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            upload_lock_manager.set_cached_meta(f"folder_{folder_id}", data)
            return data
    except Exception as e:
        logger.error(f"Error reading folder metadata for folder_id={folder_id}: {e}")
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
        # Serialize sets to lists for disk persistence
        if isinstance(serializable.get("received_chunks"), (set, list)):
            serializable["received_chunks"] = sorted(list(serializable["received_chunks"]))
        if isinstance(serializable.get("in_flight_chunks"), (set, list)):
            serializable["in_flight_chunks"] = sorted(list(serializable["in_flight_chunks"]))
        
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

def save_folder_metadata(folder_id, metadata, write_disk=True):
    """Save folder metadata to in-memory state and atomically write to disk."""
    upload_lock_manager.set_cached_meta(f"folder_{folder_id}", metadata)
    if not write_disk:
        return True

    meta_path = get_folder_metadata_path(folder_id)
    if not meta_path:
        return False
    tmp_path = f"{meta_path}.tmp_{uuid.uuid4().hex[:6]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
            f.flush()
            
        os.replace(tmp_path, meta_path)
        return True
    except Exception as e:
        logger.error(f"Error saving folder metadata for folder_id={folder_id}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False

def clean_expired_cache():
    """
    Scans cache directory and cleans up abandoned file and folder uploads safely with lock.
    Assembling uploads are strictly protected from ordinary cleanup to prevent deleting active multi-GB assembly.
    """
    if not os.path.exists(CACHE_DIR):
        return
    now = time.time()
    count_cleaned = 0
    try:
        for entry in os.listdir(CACHE_DIR):
            item_path = os.path.join(CACHE_DIR, entry)
            if not os.path.isdir(item_path):
                continue
            
            if entry.startswith("folder_"):
                folder_id = entry[7:]
                if not is_valid_uuid(folder_id):
                    continue
                with upload_lock_manager.acquire(f"folder_{folder_id}"):
                    meta = load_folder_metadata(folder_id)
                    status = meta.get("status", "unknown") if meta else "unknown"
                    if status == "assembling":
                        continue
                    last_activity = meta.get("updated_at") if meta else None
                    if last_activity is None:
                        try:
                            last_activity = os.path.getmtime(item_path)
                        except OSError:
                            continue
                    if now - last_activity > UPLOAD_CACHE_TIMEOUT:
                        logger.info(f"FOLDER CACHE EXPIRED: folder_id={folder_id}. Deleting cache.")
                        upload_lock_manager.remove_cached_meta(f"folder_{folder_id}")
                        try:
                            shutil.rmtree(item_path, ignore_errors=True)
                            count_cleaned += 1
                        except Exception as err:
                            logger.error(f"Failed to remove expired folder cache for {entry}: {err}")
            elif is_valid_uuid(entry):
                with upload_lock_manager.acquire(entry):
                    meta = load_metadata(entry)
                    status = meta.get("status", "unknown") if meta else "unknown"
                    if status == "assembling":
                        continue
                    last_activity = meta.get("updated_at") if meta else None
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

        .btn-outline {
            background: transparent;
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
        }

        .btn-outline:hover, .btn-outline:focus-visible {
            background: var(--bg-surface-hover);
            border-color: var(--border-strong);
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

        .btn-danger-outline {
            background: transparent;
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.25);
        }

        .btn-danger-outline:hover, .btn-danger-outline:focus-visible {
            background: rgba(239, 68, 68, 0.15);
            border-color: rgba(239, 68, 68, 0.45);
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
        .badge-queued { background: rgba(140, 147, 164, 0.12); color: var(--text-secondary); }

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

        /* Dedicated Horizontal Filter Scroll Container */
        .filter-scroll-wrapper {
            position: relative;
            width: 100%;
            max-width: 100%;
            min-width: 0;
            display: flex;
            align-items: center;
        }

        .filter-pills-bar {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            overflow-x: auto;
            overflow-y: hidden;
            width: 100%;
            max-width: 100%;
            min-width: 0;
            white-space: nowrap;
            scrollbar-width: none;
            -ms-overflow-style: none;
            -webkit-overflow-scrolling: touch;
            touch-action: pan-x;
            overscroll-behavior-x: contain;
            padding: 0.2rem 0;
        }

        .filter-pills-bar::-webkit-scrollbar {
            display: none;
        }

        .filter-fade-right {
            position: absolute;
            right: 0;
            top: 0;
            bottom: 0;
            width: 32px;
            background: linear-gradient(to right, rgba(17, 19, 24, 0), var(--bg-surface) 90%);
            pointer-events: none;
            opacity: 1;
            transition: opacity 0.2s ease;
        }

        .filter-fade-right.hidden {
            opacity: 0;
        }

        .filter-pill {
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            color: var(--text-secondary);
            padding: 0.35rem 0.75rem;
            font-size: 0.78rem;
            font-weight: 500;
            cursor: pointer;
            transition: var(--transition-smooth);
            white-space: nowrap;
            flex: 0 0 auto;
            min-height: 34px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
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
        .file-icon-box.file-folder { background: rgba(59, 130, 246, 0.12); color: #60a5fa; border-color: rgba(59, 130, 246, 0.25); }
        .file-icon-box.file-generic { background: rgba(100, 116, 139, 0.12); color: #94a3b8; border-color: rgba(100, 116, 139, 0.2); }

        /* Folder Card Expandable Subfiles */
        .folder-subfiles-list {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            padding: 0.5rem;
            max-height: 180px;
            overflow-y: auto;
            margin-top: 0.25rem;
        }

        .folder-subfile-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.76rem;
            color: var(--text-secondary);
            padding: 0.3rem 0.45rem;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.02);
            gap: 0.5rem;
        }

        .folder-subfile-name {
            color: var(--text-primary);
            font-family: var(--font-mono);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            max-width: 65%;
        }

        /* Folder Explorer Modal */
        .folder-explorer-dialog {
            width: 100%;
            max-width: 720px;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            padding: 1.25rem 1.25rem 1rem;
        }

        .folder-breadcrumbs {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.4rem 0;
            overflow-x: auto;
            white-space: nowrap;
            font-size: 0.82rem;
            font-family: var(--font-mono);
            border-bottom: 1px solid var(--border-subtle);
            margin-bottom: 0.75rem;
            scrollbar-width: none;
        }

        .folder-breadcrumbs::-webkit-scrollbar { display: none; }

        .breadcrumb-crumb {
            color: var(--accent-text);
            cursor: pointer;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
            padding: 0.2rem 0.35rem;
            border-radius: 4px;
            transition: var(--transition-smooth);
        }

        .breadcrumb-crumb:hover {
            background: var(--accent-subtle);
            text-decoration: none;
        }

        .breadcrumb-crumb.active {
            color: var(--text-primary);
            cursor: default;
            font-weight: 600;
            background: transparent;
        }

        .breadcrumb-sep {
            color: var(--text-tertiary);
            user-select: none;
        }

        .folder-explorer-body {
            overflow-y: auto;
            flex: 1 1 auto;
            max-height: calc(85vh - 200px);
            display: flex;
            flex-direction: column;
            gap: 0.45rem;
            padding-right: 0.25rem;
        }

        .folder-explorer-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.55rem 0.75rem;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-sm);
            gap: 0.75rem;
            transition: var(--transition-smooth);
        }

        .folder-explorer-item:hover {
            background: var(--bg-surface-hover);
            border-color: var(--border-strong);
        }

        .folder-item-left {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            min-width: 0;
            flex: 1 1 auto;
        }

        .folder-item-info {
            display: flex;
            flex-direction: column;
            gap: 0.12rem;
            min-width: 0;
        }

        .folder-item-name {
            font-size: 0.84rem;
            font-weight: 500;
            color: var(--text-primary);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .folder-item-meta {
            font-size: 0.74rem;
            color: var(--text-secondary);
        }

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
            padding: 0.75rem;
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            width: 220px;
            height: 220px;
            max-width: 100%;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            overflow: hidden;
        }

        .qr-card svg, .qr-card img {
            width: 100%;
            height: 100%;
            display: block;
        }

        .qr-unavailable-text {
            color: #545b6d;
            font-size: 0.85rem;
            font-weight: 500;
            text-align: center;
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

            .filter-scroll-wrapper {
                margin: 0;
            }

            .filter-pill {
                min-height: 38px;
                padding: 0.4rem 0.85rem;
                font-size: 0.8rem;
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

        <div class="dropzone-container" id="dropzone" tabindex="0" role="button" aria-label="File drop area. Drop files or folders to share" onclick="document.getElementById('fileInput').click()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();document.getElementById('fileInput').click();}">
            <div class="dropzone-icon-box">
                <svg class="svg-icon" style="width: 20px; height: 20px;" viewBox="0 0 24 24">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
            </div>
            <div class="dropzone-text-group">
                <div class="dropzone-title">
                    <span class="desktop-text">Drop files or folders here to share</span>
                    <span class="mobile-text">Select files or folders to share</span>
                </div>
                <div class="dropzone-subtitle">Chunked · Resumable · Folder Structure Preserved</div>
            </div>
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; z-index: 2;" onclick="event.stopPropagation()">
                <button type="button" class="btn btn-primary btn-sm" onclick="document.getElementById('fileInput').click()">Browse files</button>
                <button type="button" class="btn btn-outline btn-sm" onclick="triggerFolderInput()">Browse folder</button>
            </div>
            <input type="file" id="fileInput" class="file-input" multiple onchange="handleFileSelection(event)">
            <input type="file" id="folderInput" class="file-input" webkitdirectory directory multiple onchange="handleFolderSelection(event)">
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
                        <input type="search" id="fileSearch" class="search-input" placeholder="Search files and folders..." aria-label="Search available files" oninput="applyFilters()">
                    </div>
                </div>

                <div class="filter-scroll-wrapper">
                    <div class="filter-pills-bar" id="filterPillsBar" role="tablist" aria-label="File Category Filters" onscroll="updateFilterScrollIndicator()">
                        <button class="filter-pill active" data-category="all" aria-pressed="true" onclick="setCategoryFilter('all', this)">All</button>
                        <button class="filter-pill" data-category="folders" aria-pressed="false" onclick="setCategoryFilter('folders', this)">Folders</button>
                        <button class="filter-pill" data-category="images" aria-pressed="false" onclick="setCategoryFilter('images', this)">Images</button>
                        <button class="filter-pill" data-category="videos" aria-pressed="false" onclick="setCategoryFilter('videos', this)">Videos</button>
                        <button class="filter-pill" data-category="audio" aria-pressed="false" onclick="setCategoryFilter('audio', this)">Audio</button>
                        <button class="filter-pill" data-category="documents" aria-pressed="false" onclick="setCategoryFilter('documents', this)">Documents</button>
                        <button class="filter-pill" data-category="archives" aria-pressed="false" onclick="setCategoryFilter('archives', this)">Archives</button>
                        <button class="filter-pill" data-category="code" aria-pressed="false" onclick="setCategoryFilter('code', this)">Code</button>
                        <button class="filter-pill" data-category="applications" aria-pressed="false" onclick="setCategoryFilter('applications', this)">Applications</button>
                        <button class="filter-pill" data-category="other" aria-pressed="false" onclick="setCategoryFilter('other', this)">Other</button>
                    </div>
                    <div class="filter-fade-right" id="filterFadeRight" aria-hidden="true"></div>
                </div>
            </div>

            <div id="noFilterMatches" class="empty-state" style="display: none;">
                <div class="empty-state-title" id="emptyStateTitle">No matching items</div>
                <div class="empty-state-subtitle" id="emptyStateSubtitle">Try another search query or file category.</div>
            </div>

            <table class="file-table" id="fileTable">
                <thead>
                    <tr>
                        <th scope="col">Name</th>
                        <th scope="col" class="desktop-only-col" style="width: 100px;">Size</th>
                        <th scope="col" class="desktop-only-col" style="width: 120px;">Added</th>
                        <th scope="col" style="width: 180px; text-align: right;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {% for f in files %}
                    <tr class="file-row" data-filename="{{ f.name|lower }}" data-type="{{ f.type_info.label|lower }}" data-category="{{ f.type_info.category }}">
                        <td>
                            <div class="file-name-cell">
                                <div class="file-icon-box {{ f.type_info.badge_class }}" aria-hidden="true">
                                    {% if f.is_folder %}
                                        <svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                                    {% elif f.type_info.icon == 'video' %}
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
                                    <span class="file-name-text" title="{{ f.name }}">{{ f.name }}{% if f.is_folder %}/{% endif %}</span>
                                    <span class="file-type-subtext">{% if f.is_folder %}Folder · {{ f.size_str }} · {{ f.file_count }} files · {{ f.mtime_str }}{% else %}{{ f.type_info.label }} · {{ f.size_str }} · {{ f.mtime_str }}{% endif %}</span>
                                </div>
                            </div>
                        </td>
                        <td class="file-size-cell desktop-only-col">{{ f.size_str }}</td>
                        <td class="file-date-cell desktop-only-col">{{ f.mtime_str }}</td>
                        <td style="text-align: right;">
                            {% if f.is_folder %}
                            <div style="display: flex; gap: 0.4rem; justify-content: flex-end; flex-wrap: wrap;">
                                <button type="button" class="btn btn-sm btn-primary" onclick="openFolderModal('{{ f.name|e }}')" aria-label="Open folder {{ f.name }}">
                                    <svg class="svg-icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                                    <span>Open</span>
                                </button>
                                <a href="/download/zip/{{ f.name|e }}" class="btn btn-sm btn-outline" download aria-label="Download ZIP of {{ f.name }}">
                                    <svg class="svg-icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
                                    <span>ZIP</span>
                                </a>
                            </div>
                            {% else %}
                            <a href="/download/{{ f.name|e }}" class="btn btn-sm" download aria-label="Download {{ f.name }}">
                                <svg class="svg-icon" viewBox="0 0 24 24">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
                                </svg>
                                <span>Download</span>
                            </a>
                            {% endif %}
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

        <div class="qr-card" id="qrContainer">
            {% if qr_svg %}
                {{ qr_svg|safe }}
            {% else %}
                <div class="qr-unavailable-text">QR code unavailable</div>
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

<!-- Interactive Folder Explorer Modal -->
<div class="modal-overlay" id="folderModal" onclick="closeFolderModal(event)">
    <div class="modal-dialog folder-explorer-dialog" onclick="event.stopPropagation()">
        <div class="modal-header" style="width: 100%; display: flex; justify-content: space-between; align-items: center; gap: 0.75rem;">
            <div style="display: flex; align-items: center; gap: 0.5rem; min-width: 0;">
                <div class="file-icon-box file-folder" style="width: 30px; height: 30px;">
                    <svg viewBox="0 0 24 24" style="width: 15px; height: 15px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                </div>
                <span class="modal-title" id="folderModalTitle" style="font-size: 1rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">Folder Explorer</span>
            </div>
            <div style="display: flex; align-items: center; gap: 0.45rem; flex-shrink: 0;">
                <a id="folderModalZipBtn" href="#" class="btn btn-sm btn-primary" download>
                    <svg class="svg-icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
                    <span>Download ZIP</span>
                </a>
                <button class="btn btn-icon-only" onclick="closeFolderModal()" aria-label="Close folder dialog">
                    <svg class="svg-icon" viewBox="0 0 24 24">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
        </div>

        <div class="folder-breadcrumbs" id="folderBreadcrumbs" style="width: 100%;"></div>

        <div class="search-box" style="width: 100%; margin-bottom: 0.75rem;">
            <svg class="search-icon svg-icon" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8"></circle>
                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <input type="search" id="folderSearchInput" class="search-input" placeholder="Search in folder..." aria-label="Search inside current folder" oninput="filterFolderModalContents()">
        </div>

        <div class="folder-explorer-body" id="folderExplorerList" style="width: 100%;"></div>
    </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
// ---------------------------------------------------------------------------
// Client Configuration
// ---------------------------------------------------------------------------
const CHUNK_SIZE = {{ default_chunk_size|default(8388608) }}; // Configured by server (8 MB default for high LAN speed)
const UPLOAD_CONCURRENCY = 4;                                  // 4 concurrent chunk workers per file
const MAX_CHUNK_RETRIES = 5;                                   // Max retry attempts per chunk with exponential backoff
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
const categoryLabelMap = {
    'all': 'matching',
    'images': 'image',
    'videos': 'video',
    'audio': 'audio',
    'documents': 'document',
    'archives': 'archive',
    'code': 'code',
    'applications': 'application',
    'other': 'other'
};

function updateFilterScrollIndicator() {
    const bar = document.getElementById('filterPillsBar');
    const fade = document.getElementById('filterFadeRight');
    if (!bar || !fade) return;
    const isAtEnd = (bar.scrollLeft + bar.clientWidth) >= (bar.scrollWidth - 6);
    if (isAtEnd || bar.scrollWidth <= bar.clientWidth) {
        fade.classList.add('hidden');
    } else {
        fade.classList.remove('hidden');
    }
}

window.addEventListener('resize', updateFilterScrollIndicator);
window.addEventListener('DOMContentLoaded', updateFilterScrollIndicator);

function setCategoryFilter(category, buttonEl) {
    currentCategory = category;
    document.querySelectorAll('.filter-pill').forEach(btn => {
        btn.classList.remove('active');
        btn.setAttribute('aria-pressed', 'false');
    });
    if (buttonEl) {
        buttonEl.classList.add('active');
        buttonEl.setAttribute('aria-pressed', 'true');
        try {
            buttonEl.scrollIntoView({
                behavior: 'smooth',
                inline: 'center',
                block: 'nearest'
            });
        } catch(e) {}
    }
    applyFilters();
    setTimeout(updateFilterScrollIndicator, 150);
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
    const emptyTitle = document.getElementById('emptyStateTitle');
    const emptySubtitle = document.getElementById('emptyStateSubtitle');
    const fileTable = document.getElementById('fileTable');

    if (noMatches) {
        if (visible === 0 && rows.length > 0) {
            noMatches.style.display = 'flex';
            if (fileTable) fileTable.style.display = 'none';

            if (emptyTitle && emptySubtitle) {
                if (query) {
                    emptyTitle.textContent = "No matching files";
                    emptySubtitle.textContent = "Try another search query or file category.";
                } else {
                    const catSingular = categoryLabelMap[currentCategory] || 'matching';
                    if (currentCategory === 'all') {
                        emptyTitle.textContent = "No files available";
                        emptySubtitle.textContent = "Upload files to get started.";
                    } else {
                        emptyTitle.textContent = `No ${catSingular} files`;
                        emptySubtitle.textContent = `There are no ${catSingular} files available.`;
                    }
                }
            }
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
    const uploader = new ChunkedUploader(file, uploadId);
    activeUploaders.set(uploader.elementId, uploader);
    uploadQueue.push(uploader);
    processUploadQueue();
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Unified Chunked Uploader Implementation (High Throughput & Optimized UI)
// ---------------------------------------------------------------------------
class ChunkedUploader {
    constructor(file, existingUploadId = null, folderId = null, relativePath = null, onProgress = null, onComplete = null) {
        this.file = file;
        this.filename = file.name;
        this.totalSize = file.size;
        this.lastModified = file.lastModified;
        this.chunkSize = CHUNK_SIZE;
        this.totalChunks = Math.ceil(this.totalSize / this.chunkSize) || 1;
        this.uploadId = existingUploadId;
        this.folderId = folderId;
        this.relativePath = relativePath || file.name;
        this.onProgress = onProgress;
        this.onComplete = onComplete;
        
        this.receivedChunks = new Set();
        this.status = 'queued'; // queued, uploading, paused, assembling, completed, cancelled, error
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
        
        // Unique DOM card ID per job
        this.elementId = 'upload-' + (this.uploadId || 'job-' + Math.random().toString(36).substr(2, 9) + '-' + Date.now().toString(36));

        // Only render standalone transfer card if not part of a folder
        if (!this.folderId) {
            this.renderCard();
        }
    }

    renderCard() {
        if (this.folderId) return;
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
                    <span class="status-badge badge-${this.status}" id="${this.elementId}-badge">${this.status.toUpperCase()}</span>
                    <button class="btn btn-sm" id="${this.elementId}-pause-btn" onclick="togglePauseUpload('${this.elementId}')" style="${this.status === 'queued' ? 'display:none;' : ''}">Pause</button>
                    <button class="btn btn-sm btn-danger" id="${this.elementId}-cancel-btn" onclick="cancelUpload('${this.elementId}')">Cancel</button>
                </div>
            </div>
            <div class="progress-track" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100" id="${this.elementId}-progress-container">
                <div class="progress-fill" id="${this.elementId}-fill" style="width: 0%"></div>
            </div>
            <div class="transfer-footer">
                <span id="${this.elementId}-progress-text">0% · 0 B / ${formatBytes(this.totalSize)}</span>
                <span id="${this.elementId}-speed-text">${this.status === 'queued' ? 'Queued in line...' : '-- MB/s · ETA: --'}</span>
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
        if (this.onProgress) {
            this.onProgress(this);
        }

        if (this.folderId) return;

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
            if (this.status === 'queued') {
                speedText.textContent = `Queued in line...`;
            } else if (this.status === 'uploading') {
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
            } else if (this.status === 'queued') {
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
        if (this.status === 'uploading' || this.status === 'assembling' || this.status === 'completed' || this.status === 'cancelled') return;
        this.status = 'uploading';
        this.errorMessage = '';
        this.updateUI();

        try {
            // Step 1: Initialize or Reconnect upload session
            if (!this.uploadId) {
                const payload = {
                    filename: this.filename,
                    total_size: this.totalSize,
                    chunk_size: this.chunkSize
                };
                if (this.folderId) {
                    payload.folder_id = this.folderId;
                    payload.relative_path = this.relativePath;
                }

                const startRes = await fetch('/upload/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
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

            // Save standalone session to localStorage
            if (!this.folderId) {
                saveUploadSession(this.uploadId, {
                    upload_id: this.uploadId,
                    filename: this.filename,
                    total_size: this.totalSize,
                    chunk_size: this.chunkSize,
                    total_chunks: this.totalChunks,
                    last_modified: this.lastModified
                });
            }

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
                if (!this.folderId) {
                    removeUploadSession(this.uploadId);
                    showToast(`Uploaded: ${this.filename}`);
                    checkAllUploadsFinished();
                }
                this.updateUI();

                if (this.onComplete) {
                    this.onComplete(this);
                }
            }

        } catch (err) {
            if (this.status === 'cancelled' || this.status === 'paused') return;
            console.error(`Upload error for ${this.filename}:`, err);
            this.status = 'error';
            this.errorMessage = err.message || "Network error occurred";
            this.updateUI();
        } finally {
            processUploadQueue();
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
                if (!this.folderId) removeUploadSession(this.uploadId);
                this.updateUI();
                return;
            }

            const statusData = await statusRes.json();
            if (!statusData.success) {
                this.status = 'error';
                this.errorMessage = statusData.error || "Upload session not found";
                if (!this.folderId) removeUploadSession(this.uploadId);
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
                if (!this.folderId) {
                    removeUploadSession(this.uploadId);
                    checkAllUploadsFinished();
                }
                this.updateUI();
                if (this.onComplete) this.onComplete(this);
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
                    if (!this.folderId) {
                        removeUploadSession(this.uploadId);
                        showToast(`Uploaded: ${this.filename}`);
                        checkAllUploadsFinished();
                    }
                    this.updateUI();
                    if (this.onComplete) this.onComplete(this);
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
                        if (!this.folderId) {
                            removeUploadSession(this.uploadId);
                            showToast(`Uploaded: ${this.filename}`);
                            checkAllUploadsFinished();
                        }
                        this.updateUI();
                        if (this.onComplete) this.onComplete(this);
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
            console.warn(`Reconciliation error for ${this.filename}:`, err);
            if (isUserResume) {
                this.status = 'error';
                this.errorMessage = "Network reconnection failed. Please retry.";
                this.updateUI();
            }
        } finally {
            this.isReconciling = false;
            processUploadQueue();
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
                const elapsedSec = (now - this.lastSpeedCheck) / 1000.0;
                if (elapsedSec >= 0.35) {
                    const instantSpeed = this.bytesSinceLastCheck / elapsedSec;
                    this.currentSpeed = (this.currentSpeed === 0) ? instantSpeed : (0.7 * this.currentSpeed + 0.3 * instantSpeed);
                    this.bytesSinceLastCheck = 0;
                    this.lastSpeedCheck = now;
                    this.requestUIUpdate();
                }
            }, 400);

            for (let w = 0; w < workerCount; w++) {
                workers.push((async () => {
                    while (queue.length > 0 && this.status === 'uploading' && this.uploadGeneration === currentGen) {
                        const chunkIndex = queue.shift();
                        if (chunkIndex === undefined) break;

                        let retries = 0;
                        let success = false;

                        while (!success && retries < MAX_CHUNK_RETRIES && this.status === 'uploading' && this.uploadGeneration === currentGen) {
                            try {
                                await this.uploadSingleChunk(chunkIndex, currentGen);
                                success = true;
                                this.receivedChunks.add(chunkIndex);
                                this.requestUIUpdate();
                            } catch (err) {
                                if (this.status !== 'uploading' || this.uploadGeneration !== currentGen) break;
                                retries++;
                                console.warn(`Retry ${retries}/${MAX_CHUNK_RETRIES} for ${this.filename} chunk ${chunkIndex}:`, err);
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
        if (!this.folderId) showToast("Transfer paused");
        processUploadQueue();
    }

    resume() {
        if (this.status !== 'paused' && this.status !== 'error') return;
        this.status = 'queued';
        this.errorMessage = '';
        this.updateUI();
        if (!this.folderId) showToast("Resuming transfer");
        processUploadQueue();
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
            if (!this.folderId) removeUploadSession(this.uploadId);
        }

        this.updateUI();
        if (!this.folderId) {
            showToast("Transfer cancelled");
            checkAllUploadsFinished();
        }
        processUploadQueue();
    }
}

// ---------------------------------------------------------------------------
// Folder Uploader Implementation (Hierarchical Progress & Aggregate Lifecycle)
// ---------------------------------------------------------------------------
class FolderUploader {
    constructor(folderName, filesData) {
        this.folderName = folderName;
        this.filesData = filesData; // [{ file, relativePath, size }]
        this.totalFiles = filesData.length;
        this.totalSize = filesData.reduce((acc, f) => acc + f.size, 0);
        
        this.folderId = null;
        this.status = 'queued'; // queued, uploading, paused, assembling, completed, cancelled, error
        this.errorMessage = '';
        this.isExpanded = false;
        
        this.childUploaders = [];
        this.uploadedBytes = 0;
        this.currentSpeed = 0;
        this.bytesSinceLastCheck = 0;
        this.lastSpeedCheck = Date.now();
        this.uiUpdatePending = false;
        
        this.elementId = 'folder-job-' + Math.random().toString(36).substr(2, 9) + '-' + Date.now().toString(36);
        this.renderCard();
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
                    <div style="display: flex; align-items: center; gap: 0.5rem;">
                        <div class="file-icon-box file-folder" style="width: 28px; height: 28px;">
                            <svg viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                        </div>
                        <div class="transfer-filename" title="${escapeHtml(this.folderName)}">${escapeHtml(this.folderName)}/</div>
                    </div>
                    <div class="transfer-meta">${this.totalFiles} files · ${formatBytes(this.totalSize)} · <span id="${this.elementId}-files-count">0/${this.totalFiles} finished</span></div>
                </div>
                <div class="transfer-actions">
                    <span class="status-badge badge-${this.status}" id="${this.elementId}-badge">${this.status.toUpperCase()}</span>
                    <button class="btn btn-sm btn-outline" onclick="toggleFolderDetails('${this.elementId}')" id="${this.elementId}-toggle-btn">Show files (${this.totalFiles})</button>
                    <button class="btn btn-sm" id="${this.elementId}-pause-btn" onclick="togglePauseFolder('${this.elementId}')" style="${this.status === 'queued' ? 'display:none;' : ''}">Pause</button>
                    <button class="btn btn-sm btn-danger" id="${this.elementId}-cancel-btn" onclick="cancelFolder('${this.elementId}')">Cancel</button>
                </div>
            </div>
            <div class="progress-track" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100" id="${this.elementId}-progress-container">
                <div class="progress-fill" id="${this.elementId}-fill" style="width: 0%"></div>
            </div>
            <div class="transfer-footer">
                <span id="${this.elementId}-progress-text">0% · 0 B / ${formatBytes(this.totalSize)}</span>
                <span id="${this.elementId}-speed-text">${this.status === 'queued' ? 'Queued in line...' : '-- MB/s · ETA: --'}</span>
            </div>
            <div class="folder-subfiles-list" id="${this.elementId}-subfiles" style="display: none;">
                ${this.filesData.map((f, idx) => `
                    <div class="folder-subfile-row" id="${this.elementId}-subfile-${idx}">
                        <span class="folder-subfile-name" title="${escapeHtml(f.relativePath)}">${escapeHtml(f.relativePath)}</span>
                        <div style="display: flex; align-items: center; gap: 0.45rem;">
                            <span>${formatBytes(f.size)}</span>
                            <span class="status-badge badge-queued" id="${this.elementId}-subfile-badge-${idx}">QUEUED</span>
                        </div>
                    </div>
                `).join('')}
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
        const filesCountText = document.getElementById(`${this.elementId}-files-count`);
        const pauseBtn = document.getElementById(`${this.elementId}-pause-btn`);
        const cancelBtn = document.getElementById(`${this.elementId}-cancel-btn`);

        let completedFiles = 0;
        let aggregateUploadedBytes = 0;
        let aggregateSpeed = 0;

        this.childUploaders.forEach((u, idx) => {
            if (u.status === 'completed') {
                completedFiles++;
                aggregateUploadedBytes += u.totalSize;
            } else {
                aggregateUploadedBytes += u.uploadedBytes;
                if (u.status === 'uploading') aggregateSpeed += u.currentSpeed;
            }

            const subBadge = document.getElementById(`${this.elementId}-subfile-badge-${idx}`);
            if (subBadge) {
                subBadge.className = `status-badge badge-${u.status}`;
                subBadge.textContent = u.status.toUpperCase();
            }
        });

        this.uploadedBytes = aggregateUploadedBytes;
        this.currentSpeed = aggregateSpeed;

        if (filesCountText) {
            filesCountText.textContent = `${completedFiles}/${this.totalFiles} finished`;
        }

        let percent = 0;
        if (this.totalSize > 0) {
            percent = Math.min(99, Math.round((this.uploadedBytes / this.totalSize) * 100));
        } else if (completedFiles === this.totalFiles) {
            percent = 100;
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

        if (speedText) {
            if (this.status === 'queued') {
                speedText.textContent = `Queued in line...`;
            } else if (this.status === 'uploading') {
                const remainingBytes = Math.max(0, this.totalSize - this.uploadedBytes);
                const etaSeconds = this.currentSpeed > 0 ? (remainingBytes / this.currentSpeed) : 0;
                speedText.textContent = `${formatBytes(this.currentSpeed)}/s · ${formatTime(etaSeconds)} remaining`;
            } else if (this.status === 'assembling') {
                speedText.textContent = `Publishing and verifying folder structure...`;
            } else if (this.status === 'completed') {
                speedText.textContent = `Folder uploaded and published`;
            } else if (this.status === 'cancelled') {
                speedText.textContent = `Folder transfer cancelled`;
            } else if (this.status === 'paused') {
                speedText.textContent = `Folder transfer paused`;
            } else if (this.status === 'error') {
                speedText.textContent = `${this.errorMessage || 'Folder transfer failed'}`;
            }
        }

        if (badge) {
            badge.className = `status-badge badge-${this.status}`;
            badge.textContent = this.status.toUpperCase();
        }

        if (pauseBtn) {
            if (this.status === 'completed' || this.status === 'cancelled' || this.status === 'assembling') {
                pauseBtn.style.display = 'none';
            } else if (this.status === 'queued') {
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

    toggleDetails() {
        this.isExpanded = !this.isExpanded;
        const subfiles = document.getElementById(`${this.elementId}-subfiles`);
        const toggleBtn = document.getElementById(`${this.elementId}-toggle-btn`);
        if (subfiles) subfiles.style.display = this.isExpanded ? 'flex' : 'none';
        if (toggleBtn) toggleBtn.textContent = this.isExpanded ? `Hide files` : `Show files (${this.totalFiles})`;
    }

    async start() {
        if (this.status === 'uploading' || this.status === 'assembling' || this.status === 'completed' || this.status === 'cancelled') return;
        this.status = 'uploading';
        this.errorMessage = '';
        this.updateUI();

        try {
            // Step 1: Initialize folder upload session with server authoritative manifest
            if (!this.folderId) {
                const manifest = this.filesData.map(f => ({
                    relative_path: f.relativePath,
                    size: f.size
                }));

                const startRes = await fetch('/folder/upload/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        folder_name: this.folderName,
                        total_files: this.totalFiles,
                        total_size: this.totalSize,
                        files: manifest
                    })
                });

                const startData = await startRes.json();
                if (!startData.success) {
                    throw new Error(startData.error || "Failed to initialize folder upload session");
                }

                this.folderId = startData.folder_id;
            }

            // Step 2: Instantiate ChunkedUploaders for all sub-files
            this.childUploaders = this.filesData.map(f => {
                return new ChunkedUploader(
                    f.file,
                    null,
                    this.folderId,
                    f.relativePath,
                    () => this.requestUIUpdate(),
                    (child) => this.onChildComplete(child)
                );
            });

            // Enqueue subfiles into main uploadQueue
            this.childUploaders.forEach(child => {
                activeUploaders.set(child.elementId, child);
                uploadQueue.push(child);
            });

            this.updateUI();
            processUploadQueue();

        } catch (err) {
            console.error(`Folder upload error for ${this.folderName}:`, err);
            this.status = 'error';
            this.errorMessage = err.message || "Failed to start folder upload";
            this.updateUI();
        }
    }

    async onChildComplete(child) {
        this.requestUIUpdate();
        const allCompleted = this.childUploaders.every(u => u.status === 'completed');

        if (allCompleted && this.status !== 'completed' && this.status !== 'assembling') {
            this.status = 'assembling';
            this.updateUI();

            try {
                const compRes = await fetch('/folder/upload/complete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_id: this.folderId })
                });

                const compData = await compRes.json();
                if (!compData.success) {
                    throw new Error(compData.error || "Failed to finalize folder publish");
                }

                this.status = 'completed';
                this.updateUI();
                showToast(`Folder uploaded: ${this.folderName}`);
                checkAllUploadsFinished();
            } catch (err) {
                console.error(`Error completing folder ${this.folderName}:`, err);
                this.status = 'error';
                this.errorMessage = err.message || "Failed to complete folder assembly";
                this.updateUI();
            }
        }
    }

    pause() {
        if (this.status !== 'uploading') return;
        this.status = 'paused';
        this.childUploaders.forEach(u => {
            if (u.status === 'uploading') u.pause();
        });
        this.updateUI();
        showToast("Folder upload paused");
        processUploadQueue();
    }

    resume() {
        if (this.status !== 'paused' && this.status !== 'error') return;
        this.status = 'uploading';
        this.childUploaders.forEach(u => {
            if (u.status === 'paused' || u.status === 'error') u.resume();
        });
        this.updateUI();
        showToast("Resuming folder upload");
        processUploadQueue();
    }

    async cancel() {
        this.status = 'cancelled';
        this.childUploaders.forEach(u => u.cancel());

        if (this.folderId) {
            try {
                await fetch(`/folder/upload/cancel/${this.folderId}`, { method: 'POST' });
            } catch (e) {
                console.warn("Failed to notify server of folder cancellation:", e);
            }
        }

        this.updateUI();
        showToast("Folder upload cancelled");
        processUploadQueue();
        checkAllUploadsFinished();
    }
}

// ---------------------------------------------------------------------------
// Multi-File & Folder Queue Manager & UI Event Handlers
// ---------------------------------------------------------------------------
const MAX_CONCURRENT_FILES = 2;
let uploadQueue = [];

function processUploadQueue() {
    let activeCount = 0;
    activeUploaders.forEach(uploader => {
        if (uploader.status === 'uploading' || uploader.status === 'assembling') {
            activeCount++;
        }
    });

    while (activeCount < MAX_CONCURRENT_FILES) {
        const nextUploader = uploadQueue.find(u => u.status === 'queued');
        if (!nextUploader) break;
        activeCount++;
        nextUploader.start();
    }
}

function checkAllUploadsFinished() {
    let hasPending = false;
    let completedCount = 0;
    activeUploaders.forEach(u => {
        if (u.status === 'uploading' || u.status === 'assembling' || u.status === 'queued') {
            hasPending = true;
        }
        if (u.status === 'completed') {
            completedCount++;
        }
    });

    if (!hasPending && completedCount > 0) {
        setTimeout(() => {
            window.location.reload();
        }, 1200);
    }
}

function enqueueFiles(fileList) {
    if (!fileList || fileList.length === 0) return;
    const files = Array.from(fileList);

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (!file) continue;
        const uploader = new ChunkedUploader(file);
        activeUploaders.set(uploader.elementId, uploader);
        uploadQueue.push(uploader);
    }

    processUploadQueue();
}

function handleFileSelection(event) {
    const files = event.target.files;
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    event.target.value = '';
    enqueueFiles(fileArray);
}

function triggerFolderInput() {
    const input = document.getElementById('folderInput');
    if (!input) return;
    if (input.webkitdirectory === undefined && !('directory' in input)) {
        showToast("Folder upload is not supported in this browser. Please select files.");
        document.getElementById('fileInput').click();
        return;
    }
    input.click();
}

function handleFolderSelection(event) {
    const files = event.target.files;
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    event.target.value = '';

    // Group files by root directory name
    const folderGroups = new Map();
    fileArray.forEach(file => {
        const fullRel = file.webkitRelativePath || file.name;
        const parts = fullRel.split('/');
        const folderName = parts.length > 1 ? parts[0] : 'Folder';
        const innerRel = parts.length > 1 ? parts.slice(1).join('/') : file.name;

        if (!folderGroups.has(folderName)) {
            folderGroups.set(folderName, []);
        }
        folderGroups.get(folderName).push({
            file: file,
            relativePath: innerRel,
            size: file.size
        });
    });

    folderGroups.forEach((groupFiles, folderName) => {
        const folderUploader = new FolderUploader(folderName, groupFiles);
        activeUploaders.set(folderUploader.elementId, folderUploader);
        folderUploader.start();
    });
}

function toggleFolderDetails(elementId) {
    const folderUploader = activeUploaders.get(elementId);
    if (folderUploader && folderUploader.toggleDetails) {
        folderUploader.toggleDetails();
    }
}

function togglePauseFolder(elementId) {
    const folderUploader = activeUploaders.get(elementId);
    if (folderUploader) {
        if (folderUploader.status === 'paused' || folderUploader.status === 'error') {
            folderUploader.resume();
        } else if (folderUploader.status === 'uploading') {
            folderUploader.pause();
        }
    }
}

function cancelFolder(elementId) {
    const folderUploader = activeUploaders.get(elementId);
    if (folderUploader) {
        folderUploader.cancel();
    }
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
        } else if (uploader.status === 'uploading') {
            uploader.pause();
        }
    }
}

// ---------------------------------------------------------------------------
// Drag & Drop Handling (Files and Recursive Directory Parsing)
// ---------------------------------------------------------------------------
async function scanFilesAndDirectories(dataTransfer) {
    const items = dataTransfer.items;
    const standaloneFiles = [];
    const folderGroups = new Map(); // folderName -> [{ file, relativePath, size }]

    if (items && items.length > 0 && (items[0].webkitGetAsEntry || items[0].getAsEntry)) {
        const entries = [];
        for (let i = 0; i < items.length; i++) {
            const entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : items[i].getAsEntry();
            if (entry) entries.push(entry);
        }

        async function readEntry(entry, currentPath = '') {
            if (entry.isFile) {
                return new Promise((resolve) => {
                    entry.file((file) => {
                        resolve([{ file, path: currentPath ? `${currentPath}/${file.name}` : file.name }]);
                    }, () => resolve([]));
                });
            } else if (entry.isDirectory) {
                return new Promise((resolve) => {
                    const dirReader = entry.createReader();
                    const allEntries = [];
                    function readBatch() {
                        dirReader.readEntries(async (batch) => {
                            if (!batch.length) {
                                let collected = [];
                                for (let subEntry of allEntries) {
                                    const res = await readEntry(subEntry, currentPath ? `${currentPath}/${entry.name}` : entry.name);
                                    collected = collected.concat(res);
                                }
                                resolve(collected);
                            } else {
                                allEntries.push(...batch);
                                readBatch();
                            }
                        }, () => resolve([]));
                    }
                    readBatch();
                });
            }
            return [];
        }

        for (let entry of entries) {
            if (entry.isDirectory) {
                const subFiles = await readEntry(entry, '');
                const folderName = entry.name;
                const formatted = subFiles.map(item => {
                    // strip root folder prefix
                    const parts = item.path.split('/');
                    const rel = parts.length > 1 ? parts.slice(1).join('/') : item.file.name;
                    return { file: item.file, relativePath: rel, size: item.file.size };
                });
                folderGroups.set(folderName, formatted);
            } else if (entry.isFile) {
                const fileItems = await readEntry(entry, '');
                fileItems.forEach(fi => standaloneFiles.push(fi.file));
            }
        }
    } else if (dataTransfer.files && dataTransfer.files.length > 0) {
        standaloneFiles.push(...Array.from(dataTransfer.files));
    }

    return { standaloneFiles, folderGroups };
}

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

dropzone.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.remove('dragover');

    try {
        const { standaloneFiles, folderGroups } = await scanFilesAndDirectories(e.dataTransfer);
        if (standaloneFiles.length > 0) {
            enqueueFiles(standaloneFiles);
        }
        folderGroups.forEach((groupFiles, folderName) => {
            const folderUploader = new FolderUploader(folderName, groupFiles);
            activeUploaders.set(folderUploader.elementId, folderUploader);
            folderUploader.start();
        });
    } catch (err) {
        console.error("Error reading dropped items:", err);
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            enqueueFiles(Array.from(e.dataTransfer.files));
        }
    }
});

// ---------------------------------------------------------------------------
// Interactive Folder Explorer Modal Logic
// ---------------------------------------------------------------------------
let currentExplorerPath = '';
let currentExplorerItems = [];

async function openFolderModal(folderPath) {
    const modal = document.getElementById('folderModal');
    if (!modal) return;
    modal.style.display = 'flex';
    document.getElementById('folderSearchInput').value = '';
    await loadFolderContents(folderPath);
}

function closeFolderModal(event) {
    const modal = document.getElementById('folderModal');
    if (modal) modal.style.display = 'none';
}

async function loadFolderContents(folderPath) {
    currentExplorerPath = folderPath;
    const listEl = document.getElementById('folderExplorerList');
    const titleEl = document.getElementById('folderModalTitle');
    const zipBtn = document.getElementById('folderModalZipBtn');
    const breadcrumbsEl = document.getElementById('folderBreadcrumbs');

    if (listEl) listEl.innerHTML = '<div style="padding: 1.5rem; text-align: center; color: var(--text-secondary); font-size: 0.82rem;">Loading folder contents...</div>';

    const rootFolder = folderPath.split('/')[0];
    if (titleEl) titleEl.textContent = rootFolder;
    if (zipBtn) zipBtn.href = `/download/zip/${encodeURIComponent(rootFolder)}`;

    try {
        const res = await fetch(`/folder/contents/${encodeURI(folderPath)}`);
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || "Failed to load directory");
        }
        const data = await res.json();
        currentExplorerItems = data.items || [];
        renderFolderBreadcrumbs(data.breadcrumbs || []);
        renderFolderItems(currentExplorerItems);
    } catch (err) {
        if (listEl) {
            listEl.innerHTML = `<div style="padding: 1.5rem; text-align: center; color: #f87171; font-size: 0.82rem;">${escapeHtml(err.message)}</div>`;
        }
    }
}

function renderFolderBreadcrumbs(breadcrumbs) {
    const el = document.getElementById('folderBreadcrumbs');
    if (!el) return;

    let html = '';
    breadcrumbs.forEach((crumb, idx) => {
        const isLast = idx === breadcrumbs.length - 1;
        if (idx > 0) html += '<span class="breadcrumb-sep">/</span>';
        if (isLast) {
            html += `<span class="breadcrumb-crumb active">${escapeHtml(crumb.name)}</span>`;
        } else {
            html += `<span class="breadcrumb-crumb" onclick="loadFolderContents('${escapeHtml(crumb.path)}')">${escapeHtml(crumb.name)}</span>`;
        }
    });
    el.innerHTML = html;
}

function renderFolderItems(items) {
    const listEl = document.getElementById('folderExplorerList');
    if (!listEl) return;

    if (!items || items.length === 0) {
        listEl.innerHTML = '<div style="padding: 2rem 1rem; text-align: center; color: var(--text-tertiary); font-size: 0.82rem;">This folder is empty</div>';
        return;
    }

    listEl.innerHTML = items.map(item => {
        if (item.is_folder) {
            return `
                <div class="folder-explorer-item" data-name="${escapeHtml(item.name.toLowerCase())}">
                    <div class="folder-item-left" style="cursor: pointer;" onclick="loadFolderContents('${escapeHtml(item.relative_path)}')">
                        <div class="file-icon-box file-folder" style="width: 32px; height: 32px;">
                            <svg viewBox="0 0 24 24" style="width: 16px; height: 16px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                        </div>
                        <div class="folder-item-info">
                            <span class="folder-item-name">${escapeHtml(item.name)}/</span>
                            <span class="folder-item-meta">${item.file_count} files · ${item.size_str}</span>
                        </div>
                    </div>
                    <button class="btn btn-sm btn-outline" onclick="loadFolderContents('${escapeHtml(item.relative_path)}')">Open</button>
                </div>
            `;
        } else {
            return `
                <div class="folder-explorer-item" data-name="${escapeHtml(item.name.toLowerCase())}">
                    <div class="folder-item-left">
                        <div class="file-icon-box ${item.type_info ? item.type_info.badge_class : 'file-generic'}" style="width: 32px; height: 32px;">
                            <svg viewBox="0 0 24 24" style="width: 16px; height: 16px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        </div>
                        <div class="folder-item-info">
                            <span class="folder-item-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
                            <span class="folder-item-meta">${item.size_str} · ${item.mtime_str}</span>
                        </div>
                    </div>
                    <a href="/download/${encodeURI(item.relative_path)}" class="btn btn-sm" download aria-label="Download ${escapeHtml(item.name)}">
                        <svg class="svg-icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
                        <span>Download</span>
                    </a>
                </div>
            `;
        }
    }).join('');
}

function filterFolderModalContents() {
    const query = document.getElementById('folderSearchInput') ? document.getElementById('folderSearchInput').value.toLowerCase().trim() : '';
    const items = document.querySelectorAll('#folderExplorerList .folder-explorer-item');
    items.forEach(el => {
        const name = el.getAttribute('data-name') || '';
        el.style.display = (!query || name.includes(query)) ? 'flex' : 'none';
    });
}

// Page visibility and connection recovery
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        activeUploaders.forEach(uploader => {
            if ((uploader.status === 'uploading' && !uploader.isLoopRunning) || uploader.status === 'error') {
                if (uploader.reconcileAndResume) uploader.reconcileAndResume(false);
            }
        });
        processUploadQueue();
    }
});

window.addEventListener('online', () => {
    console.info("Network online detected: reconciling active uploads");
    showToast("Network restored");
    activeUploaders.forEach(uploader => {
        if ((uploader.status === 'uploading' && !uploader.isLoopRunning) || uploader.status === 'error') {
            if (uploader.reconcileAndResume) uploader.reconcileAndResume(false);
        }
    });
    processUploadQueue();
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
    """Renders the main QuickShare page listing completed files and folders in uploads/ and LAN details."""
    item_list = []
    if os.path.exists(UPLOAD_DIR):
        try:
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith('.'):
                    continue
                full_path = os.path.join(UPLOAD_DIR, fname)
                if not is_safe_path(UPLOAD_DIR, full_path):
                    continue
                    
                if os.path.isdir(full_path):
                    folder_size, file_count = get_folder_stats(full_path)
                    stat = os.stat(full_path)
                    item_list.append({
                        "name": fname,
                        "is_folder": True,
                        "file_count": file_count,
                        "size": folder_size,
                        "size_str": format_bytes(folder_size),
                        "mtime": stat.st_mtime,
                        "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
                        "type_info": {
                            "category": "folders",
                            "label": f"Folder · {file_count} files",
                            "badge_class": "file-folder",
                            "icon": "folder"
                        }
                    })
                elif os.path.isfile(full_path):
                    stat = os.stat(full_path)
                    type_info = get_file_type_info(fname)
                    item_list.append({
                        "name": fname,
                        "is_folder": False,
                        "size": stat.st_size,
                        "size_str": format_bytes(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
                        "type_info": type_info
                    })
            # Folders first, then files sorted by mtime descending
            item_list.sort(key=lambda x: (not x.get("is_folder", False), -x["mtime"]))
        except Exception as e:
            logger.error(f"Error listing uploads directory: {e}")
            
    lan_ip = get_lan_ip()
    lan_url = f"http://{lan_ip}:{PORT}"
    qr_svg = generate_qr_svg(lan_url)

    return render_template_string(
        UPLOAD_HTML,
        files=item_list,
        lan_ip=lan_ip,
        port=PORT,
        lan_url=lan_url,
        qr_svg=qr_svg,
        default_chunk_size=DEFAULT_CHUNK_SIZE
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


@app.route('/folder/upload/start', methods=['POST'])
def folder_upload_start():
    """Initializes a new folder upload session and creates authoritative manifest."""
    data = request.get_json(silent=True) or {}
    folder_name = data.get("folder_name", "").strip()
    total_files = data.get("total_files", 0)
    total_size = data.get("total_size", 0)
    files_manifest = data.get("files", [])

    if not folder_name:
        return jsonify({"success": False, "error": "Folder name is required"}), 400

    safe_folder_name = sanitize_folder_name(folder_name)
    folder_id = str(uuid.uuid4())
    cache_dir = get_folder_cache_dir(folder_id)
    os.makedirs(os.path.join(cache_dir, "files"), exist_ok=True)

    validated_files = {}
    seen_rel_paths = set()
    for item in files_manifest:
        raw_rel = item.get("relative_path", "")
        clean_rel = sanitize_relative_path(raw_rel)
        if not clean_rel:
            continue
        if clean_rel in seen_rel_paths:
            return jsonify({"success": False, "error": f"Duplicate relative path detected: '{clean_rel}'"}), 400
        seen_rel_paths.add(clean_rel)
        f_size = int(item.get("size", 0))
        validated_files[clean_rel] = {
            "relative_path": clean_rel,
            "filename": os.path.basename(clean_rel),
            "size": f_size,
            "upload_id": None,
            "status": "pending",
            "sha256": None
        }

    metadata = {
        "folder_id": folder_id,
        "folder_name": folder_name,
        "safe_folder_name": safe_folder_name,
        "total_files": len(validated_files),
        "total_size": total_size,
        "files": validated_files,
        "status": "uploading",
        "created_at": time.time(),
        "updated_at": time.time()
    }

    with upload_lock_manager.acquire(f"folder_{folder_id}"):
        if not save_folder_metadata(folder_id, metadata, write_disk=True):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "Failed to initialize folder metadata"}), 500

    logger.info(f"FOLDER UPLOAD START: folder_id={folder_id}, name='{safe_folder_name}', files={len(validated_files)}, size={total_size} bytes")
    return jsonify({
        "success": True,
        "folder_id": folder_id,
        "folder_name": safe_folder_name,
        "total_files": len(validated_files),
        "total_size": total_size
    }), 201


@app.route('/upload/start', methods=['POST'])
def upload_start():
    """Initializes a new upload session, creates cache/<upload_id>/ and metadata.json."""
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "").strip()
    total_size = data.get("total_size")
    chunk_size = data.get("chunk_size", DEFAULT_CHUNK_SIZE)
    file_hash = data.get("file_hash", "")
    folder_id = data.get("folder_id")
    relative_path = data.get("relative_path")

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

    clean_rel = sanitize_relative_path(relative_path or filename) if folder_id else None

    metadata = {
        "upload_id": upload_id,
        "filename": filename,
        "safe_filename": safe_name,
        "folder_id": folder_id,
        "relative_path": clean_rel,
        "total_size": total_size,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "received_chunks": set(),
        "in_flight_chunks": set(),
        "file_hash": file_hash,
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "uploading"
    }

    if folder_id:
        if not is_valid_uuid(folder_id):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "Invalid folder_id format"}), 400
        folder_cache = get_folder_cache_dir(folder_id)
        if not folder_cache or not os.path.exists(folder_cache):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return jsonify({"success": False, "error": "Folder upload session not found or expired"}), 404

        with upload_lock_manager.acquire(f"folder_{folder_id}"):
            f_meta = load_folder_metadata(folder_id)
            if f_meta and clean_rel in f_meta.get("files", {}):
                f_meta["files"][clean_rel]["upload_id"] = upload_id
                f_meta["files"][clean_rel]["status"] = "uploading"
                f_meta["updated_at"] = time.time()
                save_folder_metadata(folder_id, f_meta, write_disk=True)

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

    # -----------------------
    # PHASE A - SHORT LOCK: reserve chunk index
    # -----------------------
    try:
        with upload_lock_manager.acquire(upload_id):
            metadata = load_metadata(upload_id)
            if not metadata:
                return jsonify({"success": False, "error": "Upload session metadata not found"}), 404

            current_status = metadata.get("status")
            if current_status == "assembling":
                return jsonify({"success": False, "error": "Upload assembly is already in progress"}), 409
            if current_status in ["cancelled", "completed", "failed"]:
                return jsonify({"success": False, "error": f"Upload session is {current_status}"}), 400

            # Idempotent fast path: already recorded as received
            if chunk_index in metadata.get("received_chunks", set()):
                return jsonify({"success": True, "upload_id": upload_id, "chunk_index": chunk_index, "received_count": len(metadata.get("received_chunks", []))}), 200

            # Reserve as in-flight so upload_complete can account for it
            inflight = metadata.get("in_flight_chunks") or set()
            inflight.add(chunk_index)
            metadata["in_flight_chunks"] = inflight
            metadata["updated_at"] = time.time()
            save_metadata(upload_id, metadata, write_disk=False)
    except Exception as e:
        logger.error(f"Error reserving chunk {chunk_index} for upload_id={upload_id}: {e}")
        return jsonify({"success": False, "error": "Internal server error"}), 500

    # -----------------------
    # PHASE B - NO LOCK: write chunk to disk
    # -----------------------
    write_error = None
    try:
        chunk_file.save(temp_chunk_path)
        actual_chunk_size = os.path.getsize(temp_chunk_path)

        if actual_chunk_size != expected_chunk_size:
            write_error = (400, {"success": False, "error": f"Chunk size mismatch: expected {expected_chunk_size} B, got {actual_chunk_size} B"})
            try:
                os.remove(temp_chunk_path)
            except OSError:
                pass
        else:
            os.replace(temp_chunk_path, chunk_path)
    except Exception as e:
        logger.error(f"Error saving chunk {chunk_index} for upload_id={upload_id}: {e}")
        write_error = (500, {"success": False, "error": "Internal error storing chunk"})

    if write_error is not None:
        try:
            with upload_lock_manager.acquire(upload_id):
                meta = load_metadata(upload_id)
                if meta and "in_flight_chunks" in meta and chunk_index in meta["in_flight_chunks"]:
                    meta["in_flight_chunks"].discard(chunk_index)
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta, write_disk=False)
        except Exception:
            pass
        return jsonify(write_error[1]), write_error[0]

    # -----------------------
    # PHASE C - SHORT LOCK: finalize reservation -> received
    # -----------------------
    try:
        with upload_lock_manager.acquire(upload_id):
            meta = load_metadata(upload_id)
            if not meta:
                try:
                    os.remove(chunk_path)
                except OSError:
                    pass
                return jsonify({"success": False, "error": "Upload session metadata not found"}), 404

            if meta.get("status") == "cancelled":
                try:
                    if os.path.exists(chunk_path):
                        os.remove(chunk_path)
                except OSError:
                    pass
                if "in_flight_chunks" in meta:
                    meta["in_flight_chunks"].discard(chunk_index)
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta, write_disk=False)
                return jsonify({"success": False, "error": "Upload was cancelled"}), 400

            if chunk_index in meta.get("received_chunks", set()):
                if "in_flight_chunks" in meta:
                    meta["in_flight_chunks"].discard(chunk_index)
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta, write_disk=False)
                return jsonify({"success": True, "upload_id": upload_id, "chunk_index": chunk_index, "received_count": len(meta.get("received_chunks", []))}), 200

            recv = meta.get("received_chunks") or set()
            recv.add(chunk_index)
            meta["received_chunks"] = recv
            if "in_flight_chunks" in meta:
                meta["in_flight_chunks"].discard(chunk_index)
            meta["updated_at"] = time.time()
            save_metadata(upload_id, meta, write_disk=False)
    except Exception as e:
        logger.error(f"Error finalizing chunk {chunk_index} for upload_id={upload_id}: {e}")
        try:
            with upload_lock_manager.acquire(upload_id):
                meta = load_metadata(upload_id)
                if meta and "in_flight_chunks" in meta:
                    meta["in_flight_chunks"].discard(chunk_index)
                    meta["updated_at"] = time.time()
                    save_metadata(upload_id, meta, write_disk=False)
        except Exception:
            pass
        return jsonify({"success": False, "error": "Internal error finalizing chunk"}), 500

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

        inflight = metadata.get("in_flight_chunks", set())
        missing_chunks = []
        for i in range(total_chunks):
            chunk_path = os.path.join(chunks_dir, f"{i:06d}")
            if not os.path.exists(chunk_path) and i not in inflight:
                missing_chunks.append(i)

        if missing_chunks:
            return jsonify({
                "success": False,
                "error": f"Cannot assemble upload. Missing {len(missing_chunks)} chunks",
                "missing_chunks": missing_chunks
            }), 400

        metadata["status"] = "assembling"
        metadata["updated_at"] = time.time()
        save_metadata(upload_id, metadata, write_disk=True)
        snapshot_inflight = set(inflight)
        logger.info(f"ASSEMBLY START: upload_id={upload_id}, total_chunks={total_chunks}, expected_size={expected_size}, inflight={len(snapshot_inflight)}")

    # -------------------------------------------------------------------------
    # PHASE 2: Streaming Assembly & Hashing (NO LOCK HELD - NON-BLOCKING)
    # -------------------------------------------------------------------------
    assembled_tmp = os.path.join(cache_dir, "assembled.tmp")
    hasher = hashlib.sha256()
    assembled_size = 0
    assembly_error = None

    try:
        if snapshot_inflight:
            WAIT_INFLIGHT_SECONDS = 30
            wait_start = time.time()
            missing_now = [i for i in snapshot_inflight if not os.path.exists(os.path.join(chunks_dir, f"{i:06d}"))]
            while missing_now and (time.time() - wait_start) < WAIT_INFLIGHT_SECONDS:
                time.sleep(0.1)
                missing_now = [i for i in snapshot_inflight if not os.path.exists(os.path.join(chunks_dir, f"{i:06d}"))]
            if missing_now:
                assembly_error = f"Timed out waiting for {len(missing_now)} in-flight chunk(s) to be written before assembly"
                logger.error(f"ASSEMBLY IN-FLIGHT TIMEOUT: upload_id={upload_id} missing={missing_now}")
                raise Exception(assembly_error)

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

        calculated_hash = hasher.hexdigest()
        folder_id = latest_meta.get("folder_id")

        if folder_id:
            # File belongs to a folder upload session: stage assembled file in upload cache for folder completion
            assembled_bin = os.path.join(cache_dir, "assembled.bin")
            os.replace(assembled_tmp, assembled_bin)
            latest_meta["status"] = "completed"
            latest_meta["sha256"] = calculated_hash
            latest_meta["updated_at"] = time.time()
            save_metadata(upload_id, latest_meta, write_disk=True)

            with upload_lock_manager.acquire(f"folder_{folder_id}"):
                f_meta = load_folder_metadata(folder_id)
                rel_p = latest_meta.get("relative_path")
                if f_meta and rel_p in f_meta.get("files", {}):
                    f_meta["files"][rel_p]["status"] = "completed"
                    f_meta["files"][rel_p]["sha256"] = calculated_hash
                    f_meta["updated_at"] = time.time()
                    save_folder_metadata(folder_id, f_meta, write_disk=True)

            logger.info(f"FOLDER FILE COMPLETED: folder_id={folder_id}, rel='{rel_p}', size={assembled_size} bytes, sha256={calculated_hash}")
            return jsonify({
                "success": True,
                "filename": latest_meta["safe_filename"],
                "relative_path": rel_p,
                "size": assembled_size,
                "sha256": calculated_hash,
                "folder_id": folder_id
            }), 200
        else:
            # Standalone file: move directly to UPLOAD_DIR
            with upload_lock_manager.filename_lock():
                final_filename = get_unique_filename(UPLOAD_DIR, latest_meta["safe_filename"])
                final_dest = os.path.join(UPLOAD_DIR, final_filename)
                shutil.move(assembled_tmp, final_dest)

            latest_meta["status"] = "completed"
            latest_meta["safe_filename"] = final_filename
            latest_meta["updated_at"] = time.time()
            
            upload_lock_manager.remove_cached_meta(upload_id)
            shutil.rmtree(cache_dir, ignore_errors=True)

            logger.info(f"UPLOAD COMPLETED: upload_id={upload_id}, saved='{final_filename}', size={assembled_size} bytes, sha256={calculated_hash}")
            return jsonify({
                "success": True,
                "filename": final_filename,
                "size": assembled_size,
                "sha256": calculated_hash,
                "message": f"{final_filename} uploaded and verified successfully"
            }), 200


@app.route('/folder/upload/complete', methods=['POST'])
def folder_upload_complete():
    """Atomically finalizes and publishes an entire completed folder into uploads/."""
    data = request.get_json(silent=True) or {}
    folder_id = data.get("folder_id")

    if not folder_id or not is_valid_uuid(folder_id):
        return jsonify({"success": False, "error": "Invalid or missing folder_id"}), 400

    folder_cache = get_folder_cache_dir(folder_id)
    if not folder_cache or not os.path.exists(folder_cache):
        return jsonify({"success": False, "error": "Folder upload session not found or expired"}), 404

    with upload_lock_manager.acquire(f"folder_{folder_id}"):
        f_meta = load_folder_metadata(folder_id)
        if not f_meta:
            return jsonify({"success": False, "error": "Folder metadata not found"}), 404

        if f_meta.get("status") == "completed":
            return jsonify({"success": True, "message": "Folder already completed", "folder_name": f_meta.get("safe_folder_name")}), 200
        if f_meta.get("status") == "cancelled":
            return jsonify({"success": False, "error": "Folder upload was cancelled"}), 400

        files = f_meta.get("files", {})
        for rel_p, finfo in files.items():
            u_id = finfo.get("upload_id")
            if not u_id or finfo.get("status") != "completed":
                return jsonify({"success": False, "error": f"Cannot complete folder: '{rel_p}' is not finished ({finfo.get('status')})"}), 400

            file_cache_dir = get_upload_cache_dir(u_id)
            bin_path = os.path.join(file_cache_dir, "assembled.bin") if file_cache_dir else None
            if not bin_path or not os.path.exists(bin_path):
                return jsonify({"success": False, "error": f"Missing assembled file for '{rel_p}'"}), 400

        f_meta["status"] = "assembling"
        f_meta["updated_at"] = time.time()
        save_folder_metadata(folder_id, f_meta, write_disk=True)

    with upload_lock_manager.filename_lock():
        final_folder_name = get_unique_folder_name(UPLOAD_DIR, f_meta["safe_folder_name"])
        final_folder_dir = os.path.join(UPLOAD_DIR, final_folder_name)
        os.makedirs(final_folder_dir, exist_ok=True)

        for rel_p, finfo in files.items():
            u_id = finfo["upload_id"]
            file_cache_dir = get_upload_cache_dir(u_id)
            bin_path = os.path.join(file_cache_dir, "assembled.bin")
            dest_path = os.path.join(final_folder_dir, rel_p)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.move(bin_path, dest_path)

    with upload_lock_manager.acquire(f"folder_{folder_id}"):
        for rel_p, finfo in files.items():
            u_id = finfo.get("upload_id")
            if u_id:
                upload_lock_manager.remove_cached_meta(u_id)
                c_dir = get_upload_cache_dir(u_id)
                if c_dir:
                    shutil.rmtree(c_dir, ignore_errors=True)

        upload_lock_manager.remove_cached_meta(f"folder_{folder_id}")
        shutil.rmtree(folder_cache, ignore_errors=True)

    logger.info(f"FOLDER UPLOAD COMPLETED: folder_id={folder_id}, saved='{final_folder_name}', files={len(files)}")
    return jsonify({
        "success": True,
        "folder_name": final_folder_name,
        "total_files": len(files),
        "total_size": f_meta["total_size"],
        "message": f"Folder {final_folder_name} uploaded and published successfully"
    }), 200


@app.route('/folder/upload/cancel/<path:folder_id>', methods=['POST', 'DELETE'])
def folder_upload_cancel(folder_id):
    """Explicitly cancels an in-progress folder upload and completely purges its cache."""
    if not is_valid_uuid(folder_id):
        return jsonify({"success": False, "error": "Invalid folder_id format"}), 400
    folder_cache = get_folder_cache_dir(folder_id)
    if not folder_cache:
        return jsonify({"success": False, "error": "Invalid folder path"}), 400

    with upload_lock_manager.acquire(f"folder_{folder_id}"):
        f_meta = load_folder_metadata(folder_id)
        if f_meta:
            f_meta["status"] = "cancelled"
            f_meta["updated_at"] = time.time()
            save_folder_metadata(folder_id, f_meta, write_disk=False)
            for rel_p, finfo in f_meta.get("files", {}).items():
                u_id = finfo.get("upload_id")
                if u_id:
                    upload_lock_manager.remove_cached_meta(u_id)
                    c_dir = get_upload_cache_dir(u_id)
                    if c_dir:
                        shutil.rmtree(c_dir, ignore_errors=True)

        upload_lock_manager.remove_cached_meta(f"folder_{folder_id}")
        if os.path.exists(folder_cache):
            shutil.rmtree(folder_cache, ignore_errors=True)

    logger.info(f"FOLDER UPLOAD CANCELLED: folder_id={folder_id}. Cache purged.")
    return jsonify({"success": True, "message": "Folder upload cancelled and cache purged"}), 200


@app.route('/folder/status/<path:folder_id>', methods=['GET'])
def folder_status(folder_id):
    """Returns authoritative status of an in-progress folder upload session."""
    if not is_valid_uuid(folder_id):
        return jsonify({"success": False, "error": "Invalid folder_id format"}), 400
    folder_cache = get_folder_cache_dir(folder_id)
    if not folder_cache or not os.path.exists(folder_cache):
        return jsonify({"success": False, "error": "Folder upload not found or expired"}), 404

    with upload_lock_manager.acquire(f"folder_{folder_id}"):
        f_meta = load_folder_metadata(folder_id)
        if not f_meta:
            return jsonify({"success": False, "error": "Folder metadata not found"}), 404

        return jsonify({
            "success": True,
            "folder_id": folder_id,
            "folder_name": f_meta["folder_name"],
            "safe_folder_name": f_meta["safe_folder_name"],
            "total_files": f_meta["total_files"],
            "total_size": f_meta["total_size"],
            "status": f_meta.get("status", "uploading"),
            "files": f_meta.get("files", {})
        }), 200


@app.route('/folder/contents/<path:folder_path>', methods=['GET'])
def folder_contents(folder_path):
    """Returns the contents of a directory inside uploads/ for in-browser Folder Explorer."""
    clean_path = folder_path.strip().strip('/')
    target_dir = os.path.join(UPLOAD_DIR, clean_path)

    if not is_safe_path(UPLOAD_DIR, target_dir):
        return jsonify({"success": False, "error": "Access denied: invalid path"}), 403

    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        return jsonify({"success": False, "error": "Directory not found"}), 404

    items = []
    try:
        for entry in os.listdir(target_dir):
            if entry.startswith('.'):
                continue
            full_entry_path = os.path.join(target_dir, entry)
            rel_from_uploads = os.path.relpath(full_entry_path, UPLOAD_DIR).replace('\\', '/')

            if os.path.isdir(full_entry_path):
                sub_size, sub_count = get_folder_stats(full_entry_path)
                stat = os.stat(full_entry_path)
                items.append({
                    "name": entry,
                    "relative_path": rel_from_uploads,
                    "is_folder": True,
                    "file_count": sub_count,
                    "size": sub_size,
                    "size_str": format_bytes(sub_size),
                    "mtime": stat.st_mtime,
                    "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
                    "type_info": {
                        "category": "folders",
                        "label": f"Folder · {sub_count} files",
                        "badge_class": "file-folder",
                        "icon": "folder"
                    }
                })
            elif os.path.isfile(full_entry_path):
                stat = os.stat(full_entry_path)
                t_info = get_file_type_info(entry)
                items.append({
                    "name": entry,
                    "relative_path": rel_from_uploads,
                    "is_folder": False,
                    "size": stat.st_size,
                    "size_str": format_bytes(stat.st_size),
                    "mtime": stat.st_mtime,
                    "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y"),
                    "type_info": t_info
                })
        items.sort(key=lambda x: (not x["is_folder"], x["name"].lower()))
    except Exception as e:
        logger.error(f"Error listing folder contents for {folder_path}: {e}")
        return jsonify({"success": False, "error": "Failed to read folder contents"}), 500

    parts = [p for p in clean_path.split('/') if p]
    breadcrumbs = []
    accum = ""
    for p in parts:
        accum = f"{accum}/{p}" if accum else p
        breadcrumbs.append({"name": p, "path": accum})

    return jsonify({
        "success": True,
        "current_path": clean_path,
        "breadcrumbs": breadcrumbs,
        "items": items
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


@app.route('/download/<path:filepath>')
def download_file(filepath):
    """
    Securely serves completed files strictly from uploads/.
    Supports top-level files ('report.pdf') and nested files inside folders ('MyProject/src/main.py').
    Supports HTTP Range requests (RFC 7233 / 9110), streaming, and safe path resolution.
    Never serves from cache/ or temporary files.
    """
    clean_path = filepath.strip().strip('/')
    target_path = os.path.join(UPLOAD_DIR, clean_path)

    if not is_safe_path(UPLOAD_DIR, target_path):
        abort(403)

    if not os.path.exists(target_path) or not os.path.isfile(target_path):
        abort(404)

    directory = os.path.dirname(target_path)
    filename = os.path.basename(target_path)
    return send_from_directory(directory, filename, as_attachment=True, conditional=True)


@app.route('/download/zip/<path:folder_name>')
def download_folder_zip(folder_name):
    """
    Generates and streams a ZIP containing the complete folder directory structure.
    Temporary ZIP files are created in CACHE_DIR and cleaned up safely upon response completion.
    """
    clean_folder = folder_name.strip().strip('/')
    target_folder = os.path.join(UPLOAD_DIR, clean_folder)

    if not is_safe_path(UPLOAD_DIR, target_folder):
        abort(403)

    if not os.path.exists(target_folder) or not os.path.isdir(target_folder):
        abort(404)

    zip_temp_id = f"zip_{uuid.uuid4().hex}"
    temp_zip_path = os.path.join(CACHE_DIR, f"{zip_temp_id}.zip")

    try:
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            base_folder_name = os.path.basename(target_folder)
            for root, dirs, files in os.walk(target_folder):
                for f in files:
                    if f.startswith('.'):
                        continue
                    full_file_path = os.path.join(root, f)
                    if not is_safe_path(target_folder, full_file_path):
                        continue
                    rel_inside = os.path.relpath(full_file_path, target_folder)
                    arcname = os.path.join(base_folder_name, rel_inside).replace('\\', '/')
                    zf.write(full_file_path, arcname)

        def stream_and_remove():
            try:
                with open(temp_zip_path, 'rb') as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    if os.path.exists(temp_zip_path):
                        os.remove(temp_zip_path)
                except OSError:
                    pass

        safe_zip_filename = f"{secure_filename(os.path.basename(target_folder)) or 'folder'}.zip"
        response = Response(stream_and_remove(), mimetype='application/zip')
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_zip_filename}"'
        if os.path.exists(temp_zip_path):
            response.headers['Content-Length'] = str(os.path.getsize(temp_zip_path))
        return response
    except Exception as e:
        logger.error(f"Error creating ZIP for folder {folder_name}: {e}")
        if os.path.exists(temp_zip_path):
            try:
                os.remove(temp_zip_path)
            except OSError:
                pass
        abort(500)


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    lan_ip = get_lan_ip()
    logger.info(f"Starting QuickShare LAN File Transfer Server on http://{lan_ip}:{PORT} (Listening on {HOST}:{PORT}, debug={DEBUG})")
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)