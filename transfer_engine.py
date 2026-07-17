import os
import posixpath
import shutil
import threading
import logging
import time

import db
from ssh_client import SSHFileClient

logger = logging.getLogger("transfer")

MAX_DIR_DEPTH = 20
REPLACE_MAX_RETRIES = 5
REPLACE_RETRY_DELAY = 0.5


class TransferEngine:
    def __init__(self, config):
        self.config = config
        self.ssh = SSHFileClient(config)
        self._running = False
        self._paused = False
        self._thread = None
        self._lock = threading.RLock()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._stop_event.set()
        self._current_file = None
        self._stats = {
            "completed_files": 0,
            "failed_files": 0,
            "speed": 0,
        }

    def connect(self):
        self.ssh.connect()
        db.add_log("INFO", f"Connected to {self.config['ssh']['host']}")
        return True

    def disconnect(self):
        self.ssh.disconnect()
        db.add_log("INFO", "Disconnected")

    def is_connected(self):
        return self.ssh.is_connected()

    def list_remote_files(self, path=None):
        if path is None:
            path = self.config["paths"]["source"]
        return self.ssh.list_files(path)

    def add_files_to_queue(self, remote_files, source_base=None, _depth=0):
        if _depth > MAX_DIR_DEPTH:
            logger.warning("Max directory depth exceeded, skipping deeper traversal")
            return 0

        if source_base is None:
            source_base = self.config["paths"]["source"]
        dest_base = self.config["paths"]["destination"]

        added = 0
        for item in remote_files:
            if item["is_dir"]:
                remote_dir = item["path"]
                rel = posixpath.relpath(remote_dir, source_base)
                local_dir = os.path.join(dest_base, rel.replace("/", os.sep))
                try:
                    os.makedirs(local_dir, exist_ok=True)
                except OSError as e:
                    logger.error(f"Failed to create directory {local_dir}: {e}")
                    continue
                try:
                    sub_files = self.ssh.list_files(remote_dir)
                    added += self.add_files_to_queue(sub_files, source_base, _depth + 1)
                except Exception as e:
                    logger.error(f"Failed to list directory {remote_dir}: {e}")
            else:
                rel = posixpath.relpath(item["path"], source_base)
                local_path = os.path.join(dest_base, rel.replace("/", os.sep))
                local_dir = os.path.dirname(local_path)
                try:
                    os.makedirs(local_dir, exist_ok=True)
                except OSError as e:
                    logger.error(f"Failed to create directory {local_dir}: {e}")
                    continue

                existing = db.get_file_by_remote(item["path"])
                if existing and existing["status"] in ("complete", "pending"):
                    if os.path.exists(local_path) and os.path.getsize(local_path) == item["size"]:
                        if existing["status"] == "pending":
                            db.update_file_status(existing["id"], "complete")
                        continue
                    elif existing["status"] == "complete":
                        db.update_file_status(existing["id"], "pending")

                if os.path.exists(local_path) and os.path.getsize(local_path) == item["size"]:
                    file_id = db.add_file(item["path"], local_path, item["size"])
                    if file_id:
                        db.update_file_status(file_id, "complete")
                    logger.info(f"Skipped (exists): {item['path']}")
                    continue

                file_id = db.add_file(item["path"], local_path, item["size"], item.get("checksum"))
                if file_id:
                    added += 1
                logger.info(f"Queued: {item['path']}")

        if _depth == 0:
            db.add_log("INFO", f"Added {added} files to queue")
        return added

    def start(self):
        with self._lock:
            if self._running:
                return
            while self._thread is not None and self._thread.is_alive():
                self._lock.release()
                try:
                    self._stop_event.wait(timeout=5)
                finally:
                    self._lock.acquire()
            self._stop_event.clear()
            self._running = True
            self._paused = False
            self._pause_event.set()
            self._stats = {"completed_files": 0, "failed_files": 0, "speed": 0}
            self._thread = threading.Thread(target=self._transfer_loop, daemon=True)
            self._thread.start()
            db.add_log("INFO", "Transfer started")

    def pause(self):
        with self._lock:
            self._paused = True
            self._pause_event.clear()
            db.add_log("INFO", "Transfer paused")

    def resume(self):
        with self._lock:
            self._paused = False
            self._pause_event.set()
            db.add_log("INFO", "Transfer resumed")

    def stop(self):
        with self._lock:
            self._running = False
            self._paused = False
            self._pause_event.set()
            self._current_file = None
        db.reset_stalled_transfers()
        db.add_log("INFO", "Transfer stopped")

    def get_status(self):
        try:
            pending = db.get_pending_files()
            total_bytes = sum(f["size"] for f in pending)
            transferred = sum(f["bytes_transferred"] for f in pending)
        except Exception as e:
            logger.error(f"DB error in get_status: {e}")
            pending = []
            total_bytes = 0
            transferred = 0

        with self._lock:
            stats = dict(self._stats)
            current = self._current_file
            running = self._running
            paused = self._paused

        return {
            "running": running,
            "paused": paused,
            "connected": self.is_connected(),
            "current_file": current,
            "total_files": len(pending),
            "total_bytes": total_bytes,
            "transferred_bytes": transferred,
            "speed": stats["speed"],
            "completed_files": stats["completed_files"],
            "failed_files": stats["failed_files"],
        }

    def _transfer_loop(self):
        try:
            while True:
                with self._lock:
                    running = self._running
                    paused = self._paused
                if not running:
                    break
                if paused:
                    self._pause_event.wait()
                    with self._lock:
                        running = self._running
                    if not running:
                        break
                    continue

                try:
                    pending = db.get_pending_files()
                except Exception as e:
                    logger.error(f"DB error in transfer loop: {e}")
                    time.sleep(5)
                    continue

                if not pending:
                    with self._lock:
                        self._running = False
                    db.add_log("INFO", "All transfers complete")
                    break

                for file_info in pending:
                    with self._lock:
                        running = self._running
                        paused = self._paused
                    if not running:
                        break
                    if paused:
                        break

                    try:
                        self._transfer_file(file_info)
                    except Exception as e:
                        logger.error(f"Failed to transfer {file_info['remote_path']}: {e}")
                        try:
                            db.update_file_status(file_info["id"], "failed")
                            db.add_log("ERROR", f"Failed: {file_info['remote_path']} - {e}")
                        except Exception as db_err:
                            logger.error(f"DB update also failed: {db_err}")
                        with self._lock:
                            self._stats["failed_files"] += 1

                if not running:
                    break

        finally:
            self._stop_event.set()

    def _transfer_file(self, file_info):
        file_id = file_info["id"]
        remote_path = file_info["remote_path"]
        local_path = file_info["local_path"]
        expected_size = file_info["size"]

        with self._lock:
            self._current_file = {
                "id": file_id,
                "remote_path": remote_path,
                "local_path": local_path,
                "size": expected_size,
            }

        db.update_file_status(file_id, "transferring")
        db.add_log("INFO", f"Starting: {os.path.basename(remote_path)}")

        max_retries = self.config["transfer"]["max_retries"]
        for attempt in range(max_retries):
            with self._lock:
                running = self._running
            if not running:
                with self._lock:
                    self._current_file = None
                return

            try:
                if not self.ssh.is_connected():
                    if not self.ssh.reconnect():
                        raise Exception("Reconnect failed")

                local_tmp = local_path + ".tmp"

                existing_tmp_size = 0
                if os.path.exists(local_tmp):
                    existing_tmp_size = os.path.getsize(local_tmp)

                last_bytes = existing_tmp_size
                last_time = time.time()

                def progress_callback(bytes_transferred, total):
                    nonlocal last_bytes, last_time
                    now = time.time()
                    dt = now - last_time
                    if dt >= 1.0:
                        speed = (bytes_transferred - last_bytes) / dt
                        with self._lock:
                            self._stats["speed"] = speed
                        last_bytes = bytes_transferred
                        last_time = now
                    db.update_progress(file_id, bytes_transferred)

                self.ssh.download_file(remote_path, local_path, progress_callback)

                with self._lock:
                    if not self._running:
                        self._current_file = None
                        return

                if os.path.exists(local_tmp) and os.path.getsize(local_tmp) != expected_size:
                    actual_size = os.path.getsize(local_tmp)
                    try:
                        os.remove(local_tmp)
                    except OSError:
                        pass
                    raise Exception(
                        f"Size mismatch: expected {expected_size}, got {actual_size}"
                    )

                checksum = file_info.get("checksum")
                if not checksum:
                    checksum = self.ssh.get_checksum(remote_path)

                with self._lock:
                    if not self._running:
                        self._current_file = None
                        return

                if checksum:
                    if not self.ssh.verify_checksum(local_tmp, checksum):
                        try:
                            os.remove(local_tmp)
                        except OSError:
                            pass
                        raise Exception("Checksum mismatch")

                with self._lock:
                    if not self._running:
                        self._current_file = None
                        return

                self._atomic_replace(local_tmp, local_path)
                try:
                    db.update_file_status(file_id, "complete")
                    db.update_progress(file_id, expected_size)
                except Exception as db_err:
                    logger.error(f"DB update failed after transfer of {remote_path}: {db_err}")
                with self._lock:
                    self._stats["completed_files"] += 1
                    self._current_file = None
                db.add_log("INFO", f"Complete: {os.path.basename(remote_path)}")
                logger.info(f"Transfer complete: {remote_path}")
                return

            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {remote_path}: {e}")
                if attempt < max_retries - 1:
                    with self._lock:
                        running = self._running
                        paused = self._paused
                    if not running:
                        break
                    if paused:
                        self._pause_event.wait()
                        with self._lock:
                            running = self._running
                            paused = self._paused
                        if not running or paused:
                            break
                        continue
                    delay = self.config["transfer"]["retry_delay"] * (attempt + 1)
                    db.add_log("WARNING", f"Retry {attempt + 1} in {delay}s: {remote_path}")
                    deadline = time.time() + delay
                    while time.time() < deadline:
                        with self._lock:
                            running = self._running
                        if not running:
                            break
                        time.sleep(0.5)
                    with self._lock:
                        running = self._running
                        paused = self._paused
                    if not running:
                        break
                    if paused:
                        with self._lock:
                            self._current_file = None
                        return
                    if not self.ssh.is_connected():
                        if not self.ssh.reconnect():
                            raise Exception("Reconnect failed")

        with self._lock:
            self._current_file = None
        raise Exception(f"Failed after {max_retries} attempts")

    def _atomic_replace(self, src, dst):
        for attempt in range(REPLACE_MAX_RETRIES):
            try:
                os.replace(src, dst)
                return
            except OSError as e:
                logger.warning(f"Replace attempt {attempt + 1} failed: {e}")
                if attempt < REPLACE_MAX_RETRIES - 1:
                    time.sleep(REPLACE_RETRY_DELAY * (attempt + 1))
        try:
            shutil.move(src, dst)
            return
        except OSError:
            pass
        try:
            os.remove(dst)
            os.replace(src, dst)
            return
        except OSError:
            pass
        raise OSError(f"Failed to move {src} to {dst} after {REPLACE_MAX_RETRIES} attempts")
