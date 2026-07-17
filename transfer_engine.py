import os
import posixpath
import threading
import logging
import time

import db
from ssh_client import SSHFileClient

logger = logging.getLogger("transfer")

MAX_DIR_DEPTH = 20


class TransferEngine:
    def __init__(self, config):
        self.config = config
        self.ssh = SSHFileClient(config)
        self._running = False
        self._paused = False
        self._thread = None
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()
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
                os.makedirs(local_dir, exist_ok=True)
                try:
                    sub_files = self.ssh.list_files(remote_dir)
                    added += self.add_files_to_queue(sub_files, source_base, _depth + 1)
                except Exception as e:
                    logger.error(f"Failed to list directory {remote_dir}: {e}")
            else:
                rel = posixpath.relpath(item["path"], source_base)
                local_path = os.path.join(dest_base, rel.replace("/", os.sep))
                local_dir = os.path.dirname(local_path)
                os.makedirs(local_dir, exist_ok=True)

                existing = db.get_file_by_remote(item["path"])
                if existing and existing["status"] == "complete":
                    if os.path.exists(local_path) and os.path.getsize(local_path) == item["size"]:
                        continue

                if os.path.exists(local_path) and os.path.getsize(local_path) == item["size"] and item["size"] > 0:
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
        try:
            self.ssh.disconnect()
        except Exception:
            pass
        db.reset_stalled_transfers()
        db.add_log("INFO", "Transfer stopped")

    def get_status(self):
        try:
            pending = db.get_pending_files()
            total_bytes = sum(f["size"] for f in pending)
            transferred = sum(f["bytes_transferred"] for f in pending)
        except Exception:
            pending = []
            total_bytes = 0
            transferred = 0

        with self._lock:
            stats = dict(self._stats)
            current = self._current_file

        return {
            "running": self._running,
            "paused": self._paused,
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
        while self._running:
            if self._paused:
                self._pause_event.wait()
                if not self._running:
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
                if not self._running:
                    break
                if self._paused:
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
            if not self._running:
                break

            try:
                if not self.ssh.is_connected():
                    if not self.ssh.reconnect():
                        raise Exception("Reconnect failed")

                local_tmp = local_path + ".tmp"
                last_bytes = 0
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

                if os.path.exists(local_tmp) and os.path.getsize(local_tmp) != expected_size:
                    raise Exception(
                        f"Size mismatch: expected {expected_size}, got {os.path.getsize(local_tmp)}"
                    )

                checksum = file_info.get("checksum")
                if not checksum:
                    checksum = self.ssh.get_checksum(remote_path)

                if checksum:
                    if not self.ssh.verify_checksum(local_tmp, checksum):
                        raise Exception("Checksum mismatch")

                os.replace(local_tmp, local_path)
                db.update_file_status(file_id, "complete")
                db.update_progress(file_id, expected_size)
                with self._lock:
                    self._stats["completed_files"] += 1
                    self._stats["speed"] = 0
                    self._current_file = None
                db.add_log("INFO", f"Complete: {os.path.basename(remote_path)}")
                logger.info(f"Transfer complete: {remote_path}")
                return

            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {remote_path}: {e}")
                if attempt < max_retries - 1 and self._running:
                    delay = self.config["transfer"]["retry_delay"] * (attempt + 1)
                    db.add_log("WARNING", f"Retry {attempt + 1} in {delay}s: {remote_path}")
                    deadline = time.time() + delay
                    while time.time() < deadline and self._running and not self._paused:
                        time.sleep(0.5)
                    if not self._running or self._paused:
                        break
                    if not self.ssh.is_connected():
                        if not self.ssh.reconnect():
                            raise Exception("Reconnect failed")

        if os.path.exists(local_path + ".tmp"):
            try:
                os.remove(local_path + ".tmp")
            except Exception:
                pass
        with self._lock:
            self._current_file = None
        raise Exception(f"Failed after {max_retries} attempts")
