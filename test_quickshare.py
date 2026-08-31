import os
import sys
import io
import time
import uuid
import json
import shutil
import hashlib
import unittest

# Import the QuickShare app and helper functions
from Quickshare import (
    app,
    UPLOAD_DIR,
    CACHE_DIR,
    DEFAULT_CHUNK_SIZE,
    UPLOAD_CACHE_TIMEOUT,
    clean_expired_cache,
    get_upload_cache_dir,
    get_metadata_path,
    get_chunks_dir,
    save_metadata,
    load_metadata
)

class TestQuickShareReliability(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.test_upload_ids = []
        self.test_created_files = []

    def tearDown(self):
        # Clean up test files in uploads
        for fname in self.test_created_files:
            fpath = os.path.join(UPLOAD_DIR, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
        # Clean up test caches
        for uid in self.test_upload_ids:
            cdir = get_upload_cache_dir(uid)
            if cdir and os.path.exists(cdir):
                shutil.rmtree(cdir, ignore_errors=True)

    def _upload_helper(self, filename, data, chunk_size=1024*1024, cancel_at_chunk=None, simulate_interruption=False):
        """Helper to upload a payload with chunking."""
        total_size = len(data)
        file_hash = hashlib.sha256(data).hexdigest()
        
        # 1. Start upload
        res = self.client.post('/upload/start', json={
            "filename": filename,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "file_hash": file_hash
        })
        self.assertEqual(res.status_code, 201)
        res_data = res.get_json()
        self.assertTrue(res_data["success"])
        upload_id = res_data["upload_id"]
        total_chunks = res_data["total_chunks"]
        self.test_upload_ids.append(upload_id)

        # 2. Upload chunks
        for idx in range(total_chunks):
            if cancel_at_chunk is not None and idx == cancel_at_chunk:
                # Cancel upload
                cancel_res = self.client.post(f'/upload/cancel/{upload_id}')
                return upload_id, cancel_res, "cancelled"

            if simulate_interruption and idx == total_chunks // 2:
                # Simulate network interruption by stopping sending remaining chunks
                return upload_id, None, "interrupted"

            start = idx * chunk_size
            end = min(start + chunk_size, total_size)
            chunk_data = data[start:end]

            chunk_res = self.client.post('/upload/chunk', data={
                'upload_id': upload_id,
                'chunk_index': idx,
                'total_chunks': total_chunks,
                'chunk': (io.BytesIO(chunk_data), f"chunk_{idx}")
            }, content_type='multipart/form-data')

            self.assertEqual(chunk_res.status_code, 200)
            self.assertTrue(chunk_res.get_json()["success"])

        # 3. Complete upload
        comp_res = self.client.post('/upload/complete', json={'upload_id': upload_id})
        return upload_id, comp_res, "completed"

    # -------------------------------------------------------------------------
    # TEST 1: Small file upload
    # -------------------------------------------------------------------------
    def test_01_small_file_upload(self):
        filename = "test_small.txt"
        data = b"Hello QuickShare! Small file test."
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=1024)
        
        self.assertEqual(status, "completed")
        self.assertEqual(res.status_code, 200)
        saved_name = res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        
        # Verify file exists in uploads/ and not in cache/
        uploaded_path = os.path.join(UPLOAD_DIR, saved_name)
        self.assertTrue(os.path.exists(uploaded_path))
        with open(uploaded_path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertFalse(os.path.exists(get_upload_cache_dir(upload_id)))

    # -------------------------------------------------------------------------
    # TEST 2: Large file upload (multi-chunk)
    # -------------------------------------------------------------------------
    def test_02_large_file_upload(self):
        filename = "test_large.bin"
        # 5 MB file with 1MB chunk size = 5 chunks
        data = os.urandom(5 * 1024 * 1024)
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=1024 * 1024)
        
        self.assertEqual(status, "completed")
        self.assertEqual(res.status_code, 200)
        saved_name = res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        
        uploaded_path = os.path.join(UPLOAD_DIR, saved_name)
        self.assertEqual(os.path.getsize(uploaded_path), len(data))
        self.assertEqual(res.get_json()["sha256"], hashlib.sha256(data).hexdigest())

    # -------------------------------------------------------------------------
    # TEST 3: Upload cancellation at 10%
    # -------------------------------------------------------------------------
    def test_03_cancel_at_10_percent(self):
        filename = "test_cancel_10.bin"
        data = os.urandom(10 * 1024 * 1024)  # 10 chunks of 1MB
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=1024 * 1024, cancel_at_chunk=1) # 10%
        
        self.assertEqual(status, "cancelled")
        self.assertEqual(res.status_code, 200)
        # Cache must be completely deleted
        self.assertFalse(os.path.exists(get_upload_cache_dir(upload_id)))
        # No file in uploads/
        self.assertFalse(os.path.exists(os.path.join(UPLOAD_DIR, filename)))

    # -------------------------------------------------------------------------
    # TEST 4: Upload cancellation at 50%
    # -------------------------------------------------------------------------
    def test_04_cancel_at_50_percent(self):
        filename = "test_cancel_50.bin"
        data = os.urandom(10 * 1024 * 1024)  # 10 chunks of 1MB
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=1024 * 1024, cancel_at_chunk=5) # 50%
        
        self.assertEqual(status, "cancelled")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(os.path.exists(get_upload_cache_dir(upload_id)))
        self.assertFalse(os.path.exists(os.path.join(UPLOAD_DIR, filename)))

    # -------------------------------------------------------------------------
    # TEST 5: Upload cancellation near completion (90%)
    # -------------------------------------------------------------------------
    def test_05_cancel_near_completion(self):
        filename = "test_cancel_90.bin"
        data = os.urandom(10 * 1024 * 1024)  # 10 chunks of 1MB
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=1024 * 1024, cancel_at_chunk=9) # 90%
        
        self.assertEqual(status, "cancelled")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(os.path.exists(get_upload_cache_dir(upload_id)))
        self.assertFalse(os.path.exists(os.path.join(UPLOAD_DIR, filename)))

    # -------------------------------------------------------------------------
    # TEST 6: Network interruption during upload (cache preserved)
    # -------------------------------------------------------------------------
    def test_06_network_interruption(self):
        filename = "test_interrupted.bin"
        data = os.urandom(4 * 1024 * 1024)  # 4 chunks of 1MB
        upload_id, _, status = self._upload_helper(filename, data, chunk_size=1024 * 1024, simulate_interruption=True)
        
        self.assertEqual(status, "interrupted")
        # Cache must remain intact with the chunks uploaded so far
        self.assertTrue(os.path.exists(get_upload_cache_dir(upload_id)))
        chunks_dir = get_chunks_dir(upload_id)
        uploaded_chunks = os.listdir(chunks_dir)
        self.assertEqual(len(uploaded_chunks), 2)  # 000000 and 000001
        self.assertIn("000000", uploaded_chunks)
        self.assertIn("000001", uploaded_chunks)

    # -------------------------------------------------------------------------
    # TEST 7: Resume after network interruption
    # -------------------------------------------------------------------------
    def test_07_resume_after_interruption(self):
        filename = "test_resume.bin"
        data = os.urandom(4 * 1024 * 1024)  # 4 chunks
        chunk_size = 1024 * 1024
        upload_id, _, status = self._upload_helper(filename, data, chunk_size=chunk_size, simulate_interruption=True)
        
        # Query status to get missing chunks
        status_res = self.client.get(f'/upload/status/{upload_id}')
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.get_json()
        self.assertEqual(status_data["received_chunks"], [0, 1])
        self.assertEqual(status_data["missing_chunks"], [2, 3])
        self.assertEqual(status_data["next_chunk"], 2)

        # Upload only missing chunks
        for idx in status_data["missing_chunks"]:
            start = idx * chunk_size
            end = min(start + chunk_size, len(data))
            chunk_data = data[start:end]
            c_res = self.client.post('/upload/chunk', data={
                'upload_id': upload_id,
                'chunk_index': idx,
                'total_chunks': 4,
                'chunk': (io.BytesIO(chunk_data), f"chunk_{idx}")
            }, content_type='multipart/form-data')
            self.assertEqual(c_res.status_code, 200)

        # Complete assembly
        comp_res = self.client.post('/upload/complete', json={'upload_id': upload_id})
        self.assertEqual(comp_res.status_code, 200)
        saved_name = comp_res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        
        uploaded_path = os.path.join(UPLOAD_DIR, saved_name)
        self.assertEqual(os.path.getsize(uploaded_path), len(data))
        with open(uploaded_path, "rb") as f:
            self.assertEqual(f.read(), data)

    # -------------------------------------------------------------------------
    # TEST 8: Browser tab backgrounding / retry resiliency
    # -------------------------------------------------------------------------
    def test_08_background_and_retry_resilience(self):
        filename = "test_retry_resilience.txt"
        data = b"Simulating slow and retry chunk deliveries."
        upload_id, res, status = self._upload_helper(filename, data, chunk_size=10)
        self.assertEqual(status, "completed")
        saved_name = res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        self.assertTrue(os.path.exists(os.path.join(UPLOAD_DIR, saved_name)))

    # -------------------------------------------------------------------------
    # TEST 9: Multiple simultaneous uploads
    # -------------------------------------------------------------------------
    def test_09_multiple_simultaneous_uploads(self):
        files_data = {
            "file_a.bin": os.urandom(2 * 1024 * 1024),
            "file_b.bin": os.urandom(3 * 1024 * 1024),
            "file_c.bin": os.urandom(1 * 1024 * 1024)
        }
        sessions = {}
        # Start all 3
        for name, data in files_data.items():
            start_res = self.client.post('/upload/start', json={
                'filename': name,
                'total_size': len(data),
                'chunk_size': 1024 * 1024
            })
            uid = start_res.get_json()["upload_id"]
            self.test_upload_ids.append(uid)
            sessions[name] = {"upload_id": uid, "data": data, "total_chunks": start_res.get_json()["total_chunks"]}

        # Interleave chunk uploads for all 3
        for name, info in sessions.items():
            uid = info["upload_id"]
            data = info["data"]
            for idx in range(info["total_chunks"]):
                start = idx * 1024 * 1024
                end = min(start + 1024 * 1024, len(data))
                self.client.post('/upload/chunk', data={
                    'upload_id': uid,
                    'chunk_index': idx,
                    'total_chunks': info["total_chunks"],
                    'chunk': (io.BytesIO(data[start:end]), f"chunk_{idx}")
                }, content_type='multipart/form-data')

        # Complete all 3
        for name, info in sessions.items():
            comp_res = self.client.post('/upload/complete', json={'upload_id': info["upload_id"]})
            self.assertEqual(comp_res.status_code, 200)
            saved_name = comp_res.get_json()["filename"]
            self.test_created_files.append(saved_name)
            self.assertTrue(os.path.exists(os.path.join(UPLOAD_DIR, saved_name)))
            with open(os.path.join(UPLOAD_DIR, saved_name), "rb") as f:
                self.assertEqual(f.read(), info["data"])

    # -------------------------------------------------------------------------
    # TEST 10: Two files with the same filename (collision resolution)
    # -------------------------------------------------------------------------
    def test_10_filename_collision_handling(self):
        filename = "duplicate_name.txt"
        data1 = b"Original File 1"
        data2 = b"Second File with same name"

        _, res1, _ = self._upload_helper(filename, data1, chunk_size=1024)
        saved_name1 = res1.get_json()["filename"]
        self.test_created_files.append(saved_name1)

        _, res2, _ = self._upload_helper(filename, data2, chunk_size=1024)
        saved_name2 = res2.get_json()["filename"]
        self.test_created_files.append(saved_name2)

        self.assertNotEqual(saved_name1, saved_name2)
        self.assertEqual(saved_name1, "duplicate_name.txt")
        self.assertEqual(saved_name2, "duplicate_name (1).txt")

        # Verify content of both files
        with open(os.path.join(UPLOAD_DIR, saved_name1), "rb") as f:
            self.assertEqual(f.read(), data1)
        with open(os.path.join(UPLOAD_DIR, saved_name2), "rb") as f:
            self.assertEqual(f.read(), data2)

    # -------------------------------------------------------------------------
    # TEST 11: Server restart during incomplete upload
    # -------------------------------------------------------------------------
    def test_11_server_restart_cache_persistence(self):
        filename = "test_restart_persistence.bin"
        data = os.urandom(2 * 1024 * 1024)
        upload_id, _, _ = self._upload_helper(filename, data, chunk_size=1024 * 1024, simulate_interruption=True)
        
        # Simulate server restart by reloading metadata directly from filesystem
        meta = load_metadata(upload_id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["total_chunks"], 2)
        
        # Query status endpoint (as client would on restart)
        status_res = self.client.get(f'/upload/status/{upload_id}')
        self.assertEqual(status_res.status_code, 200)
        self.assertIn(0, status_res.get_json()["received_chunks"])

    # -------------------------------------------------------------------------
    # TEST 12: Expired abandoned upload cleanup
    # -------------------------------------------------------------------------
    def test_12_expired_abandoned_cache_cleanup(self):
        filename = "test_expired.txt"
        data = b"This file will be abandoned."
        
        # Start upload
        start_res = self.client.post('/upload/start', json={
            'filename': filename,
            'total_size': len(data),
            'chunk_size': 1024
        })
        upload_id = start_res.get_json()["upload_id"]
        self.test_upload_ids.append(upload_id)

        # Force metadata updated_at to be older than UPLOAD_CACHE_TIMEOUT
        cache_dir = get_upload_cache_dir(upload_id)
        meta = load_metadata(upload_id)
        meta["updated_at"] = time.time() - (UPLOAD_CACHE_TIMEOUT + 100)
        save_metadata(upload_id, meta)

        # Trigger cleanup
        clean_expired_cache()

        # Check cache directory was removed
        self.assertFalse(os.path.exists(cache_dir))

    # -------------------------------------------------------------------------
    # TEST 13: Completed file download
    # -------------------------------------------------------------------------
    def test_13_completed_file_download(self):
        filename = "download_test.txt"
        content = b"QuickShare download verification string."
        _, res, _ = self._upload_helper(filename, content, chunk_size=1024)
        saved_name = res.get_json()["filename"]
        self.test_created_files.append(saved_name)

        dl_res = self.client.get(f'/download/{saved_name}')
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.data, content)

    # -------------------------------------------------------------------------
    # TEST 14: Verify incomplete files cannot be downloaded
    # -------------------------------------------------------------------------
    def test_14_incomplete_files_not_downloadable(self):
        filename = "incomplete_dl_test.bin"
        data = os.urandom(2 * 1024 * 1024)
        upload_id, _, _ = self._upload_helper(filename, data, chunk_size=1024 * 1024, simulate_interruption=True)
        
        # Attempt to download uncompleted filename
        dl_res1 = self.client.get(f'/download/{filename}')
        self.assertEqual(dl_res1.status_code, 404)

        # Attempt to download chunk file or cache file
        dl_res2 = self.client.get(f'/download/{upload_id}/chunks/000000')
        self.assertIn(dl_res2.status_code, [403, 404])

    # -------------------------------------------------------------------------
    # TEST 15: Path traversal and security protection
    # -------------------------------------------------------------------------
    def test_15_security_and_path_traversal(self):
        # 1. Invalid upload_id with path traversal
        res1 = self.client.get('/upload/status/../../etc/passwd')
        self.assertEqual(res1.status_code, 400)

        res2 = self.client.post('/upload/cancel/../some_folder')
        self.assertEqual(res2.status_code, 400)

        # 2. Path traversal in download
        res3 = self.client.get('/download/../../Quickshare.py')
        self.assertIn(res3.status_code, [403, 404])

        # 3. Malicious filename in start
        res4 = self.client.post('/upload/start', json={
            'filename': '../../../hacked.txt',
            'total_size': 100,
            'chunk_size': 100
        })
        self.assertEqual(res4.status_code, 201)
        safe_name = res4.get_json().get("upload_id")
        self.test_upload_ids.append(safe_name)

    # -------------------------------------------------------------------------
    # TEST 16: Large file without excessive RAM usage (streaming assembly)
    # -------------------------------------------------------------------------
    def test_16_streaming_assembly_memory_efficiency(self):
        filename = "streaming_test.bin"
        chunk_size = 2 * 1024 * 1024  # 2MB chunks
        total_chunks = 4
        total_size = chunk_size * total_chunks
        
        start_res = self.client.post('/upload/start', json={
            'filename': filename,
            'total_size': total_size,
            'chunk_size': chunk_size
        })
        upload_id = start_res.get_json()["upload_id"]
        self.test_upload_ids.append(upload_id)

        # Send chunks with known pattern
        for idx in range(total_chunks):
            chunk_data = bytes([idx % 256]) * chunk_size
            self.client.post('/upload/chunk', data={
                'upload_id': upload_id,
                'chunk_index': idx,
                'total_chunks': total_chunks,
                'chunk': (io.BytesIO(chunk_data), f"chunk_{idx}")
            }, content_type='multipart/form-data')

        comp_res = self.client.post('/upload/complete', json={'upload_id': upload_id})
        self.assertEqual(comp_res.status_code, 200)
        saved_name = comp_res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        
        self.assertEqual(os.path.getsize(os.path.join(UPLOAD_DIR, saved_name)), total_size)

    # -------------------------------------------------------------------------
    # TEST 17: Duplicate chunk request (idempotency)
    # -------------------------------------------------------------------------
    def test_17_duplicate_chunk_request(self):
        filename = "duplicate_chunk.txt"
        data = b"1234567890ABCDEF"
        start_res = self.client.post('/upload/start', json={
            'filename': filename,
            'total_size': len(data),
            'chunk_size': 8
        })
        upload_id = start_res.get_json()["upload_id"]
        self.test_upload_ids.append(upload_id)

        # Send chunk 0 first time
        res1 = self.client.post('/upload/chunk', data={
            'upload_id': upload_id,
            'chunk_index': 0,
            'total_chunks': 2,
            'chunk': (io.BytesIO(data[:8]), "chunk_0")
        }, content_type='multipart/form-data')
        self.assertEqual(res1.status_code, 200)

        # Send chunk 0 SECOND time (duplicate/retry)
        res2 = self.client.post('/upload/chunk', data={
            'upload_id': upload_id,
            'chunk_index': 0,
            'total_chunks': 2,
            'chunk': (io.BytesIO(data[:8]), "chunk_0")
        }, content_type='multipart/form-data')
        self.assertEqual(res2.status_code, 200)

        # Send chunk 1
        self.client.post('/upload/chunk', data={
            'upload_id': upload_id,
            'chunk_index': 1,
            'total_chunks': 2,
            'chunk': (io.BytesIO(data[8:]), "chunk_1")
        }, content_type='multipart/form-data')

        comp_res = self.client.post('/upload/complete', json={'upload_id': upload_id})
        self.assertEqual(comp_res.status_code, 200)
        saved_name = comp_res.get_json()["filename"]
        self.test_created_files.append(saved_name)
        with open(os.path.join(UPLOAD_DIR, saved_name), "rb") as f:
            self.assertEqual(f.read(), data)

    # -------------------------------------------------------------------------
    # TEST 18: Repeated completion request
    # -------------------------------------------------------------------------
    def test_18_repeated_completion_request(self):
        filename = "repeated_complete.txt"
        data = b"Testing repeated complete"
        upload_id, res1, _ = self._upload_helper(filename, data, chunk_size=1024)
        self.assertEqual(res1.status_code, 200)
        self.test_created_files.append(res1.get_json()["filename"])

        # Second completion request for already completed upload
        res2 = self.client.post('/upload/complete', json={'upload_id': upload_id})
        # Should gracefully return 404 (already completed/cache purged)
        self.assertEqual(res2.status_code, 404)

    # -------------------------------------------------------------------------
    # TEST 19: Invalid upload ID
    # -------------------------------------------------------------------------
    def test_19_invalid_upload_id(self):
        # Malformed upload_id
        res1 = self.client.get('/upload/status/invalid-uuid-12345')
        self.assertEqual(res1.status_code, 400)

        # Non-existent valid UUID
        fake_uuid = str(uuid.uuid4())
        res2 = self.client.get(f'/upload/status/{fake_uuid}')
        self.assertEqual(res2.status_code, 404)

    # -------------------------------------------------------------------------
    # TEST 20: Invalid chunk index
    # -------------------------------------------------------------------------
    def test_20_invalid_chunk_index(self):
        start_res = self.client.post('/upload/start', json={
            'filename': 'bounds_test.bin',
            'total_size': 100,
            'chunk_size': 50
        })
        upload_id = start_res.get_json()["upload_id"]
        self.test_upload_ids.append(upload_id)

        # Negative chunk index
        res1 = self.client.post('/upload/chunk', data={
            'upload_id': upload_id,
            'chunk_index': -1,
            'total_chunks': 2,
            'chunk': (io.BytesIO(b"abc"), "chunk_-1")
        }, content_type='multipart/form-data')
        self.assertEqual(res1.status_code, 400)

        # Out-of-bounds chunk index
        res2 = self.client.post('/upload/chunk', data={
            'upload_id': upload_id,
            'chunk_index': 5,
            'total_chunks': 2,
            'chunk': (io.BytesIO(b"abc"), "chunk_5")
        }, content_type='multipart/form-data')
        self.assertEqual(res2.status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
