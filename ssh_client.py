import os
import posixpath
import stat
import hashlib
import logging
import time
import threading
import paramiko

logger = logging.getLogger("ssh_client")


class SSHFileClient:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.sftp = None
        self._connected = False
        self._lock = threading.Lock()
        self._reconnect_lock = threading.Lock()

        chunk_size = config["transfer"]["chunk_size"]
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError(f"chunk_size must be a positive integer, got {chunk_size}")

    def connect(self):
        with self._lock:
            if self._connected and self._check_transport():
                return True
            self._cleanup()
            return self._do_connect()

    def _do_connect(self):
        ssh_config = self.config["ssh"]
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": ssh_config["host"],
            "port": ssh_config["port"],
            "username": ssh_config["user"],
            "timeout": self.config["transfer"]["timeout"],
        }

        key_path = os.path.expanduser(ssh_config["key_path"])
        if os.path.exists(key_path):
            connect_kwargs["key_filename"] = key_path
            logger.info(f"Connecting with key: {key_path}")
        elif ssh_config.get("password"):
            connect_kwargs["password"] = ssh_config["password"]
            logger.info("Connecting with password")
        else:
            connect_kwargs["look_for_keys"] = True
            logger.info("Connecting with default SSH keys")

        try:
            self.client.connect(**connect_kwargs)
            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(30)
            self.sftp = self.client.open_sftp()
            self._connected = True
            logger.info(f"Connected to {ssh_config['host']}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._cleanup()
            raise

    def _cleanup(self):
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.sftp = None
        self.client = None
        self._connected = False

    def disconnect(self):
        with self._lock:
            self._cleanup()
            logger.info("Disconnected")

    def _check_transport(self):
        if not self._connected or not self.client:
            return False
        try:
            transport = self.client.get_transport()
            return transport and transport.is_active()
        except Exception:
            return False

    def is_connected(self):
        with self._lock:
            return self._check_transport()

    def reconnect(self):
        with self._reconnect_lock:
            with self._lock:
                max_retries = self.config["transfer"]["max_retries"]
                delay = self.config["transfer"]["retry_delay"]

            for attempt in range(max_retries):
                try:
                    logger.info(f"Reconnect attempt {attempt + 1}/{max_retries}")
                    with self._lock:
                        self._cleanup()
                        time.sleep(delay * (attempt + 1))
                        self._do_connect()
                    return True
                except Exception as e:
                    logger.warning(f"Reconnect failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))

            logger.error("Failed to reconnect after all attempts")
            return False

    def _ensure_connected(self):
        if not self.is_connected():
            self.connect()

    def _sftp_op(self, func, *args, **kwargs):
        with self._lock:
            if not self._check_transport() or not self.sftp:
                self._cleanup()
                raise ConnectionError("SFTP not connected")
            try:
                return func(*args, **kwargs)
            except (EOFError, OSError, paramiko.SSHException) as e:
                logger.error(f"SFTP operation failed: {e}")
                self._mark_disconnected()
                raise

    def _mark_disconnected(self):
        with self._lock:
            self._connected = False

    def list_files(self, remote_path):
        self._ensure_connected()
        try:
            entries = []
            for item in self._sftp_op(self.sftp.listdir_attr, remote_path):
                if item.filename.startswith("."):
                    continue
                full_path = posixpath.join(remote_path, item.filename)
                mode = item.st_mode
                is_link = mode is not None and stat.S_ISLNK(mode)
                is_dir = mode is not None and (stat.S_ISDIR(mode) or (is_link and (mode & 0o40000) != 0))
                entries.append({
                    "name": item.filename,
                    "path": full_path,
                    "is_dir": is_dir,
                    "size": item.st_size if not is_dir else 0,
                    "mtime": item.st_mtime,
                })
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return entries
        except Exception as e:
            logger.error(f"Failed to list files: {e}")
            self._mark_disconnected()
            raise

    def get_file_size(self, remote_path):
        self._ensure_connected()
        stat = self._sftp_op(self.sftp.stat, remote_path)
        return stat.st_size

    def download_file(self, remote_path, local_path, progress_callback=None):
        self._ensure_connected()
        remote_size = self.get_file_size(remote_path)

        local_tmp = local_path + ".tmp"

        if os.path.exists(local_tmp):
            local_size = os.path.getsize(local_tmp)
            if local_size == remote_size:
                return True, remote_size
            if local_size > remote_size:
                local_size = 0
        else:
            local_size = 0

        if remote_size == 0:
            with open(local_tmp, "wb"):
                pass
            return True, 0

        chunk_size = self.config["transfer"]["chunk_size"]

        try:
            with self.sftp.open(remote_path, "rb") as remote_file:
                if local_size > 0:
                    remote_file.seek(local_size)
                    logger.info(f"Resuming from {local_size} bytes")

                mode = "ab" if local_size > 0 else "wb"
                with open(local_tmp, mode) as local_file:
                    bytes_transferred = local_size
                    while True:
                        chunk = remote_file.read(chunk_size)
                        if not chunk:
                            break
                        local_file.write(chunk)
                        bytes_transferred += len(chunk)
                        if progress_callback:
                            progress_callback(bytes_transferred, remote_size)

            if bytes_transferred != remote_size:
                raise Exception(
                    f"Download incomplete: got {bytes_transferred}, expected {remote_size}"
                )

            return True, remote_size
        except Exception as e:
            logger.error(f"Download failed for {remote_path}: {e}")
            try:
                if os.path.exists(local_tmp):
                    os.remove(local_tmp)
            except Exception:
                pass
            raise

    def get_checksum(self, remote_path):
        self._ensure_connected()
        remote_size = self.get_file_size(remote_path)

        md5 = hashlib.md5()
        chunk_size = self.config["transfer"]["chunk_size"]

        with self.sftp.open(remote_path, "rb") as f:
            bytes_read = 0
            while bytes_read < remote_size:
                to_read = min(chunk_size, remote_size - bytes_read)
                chunk = f.read(to_read)
                if not chunk:
                    raise Exception(
                        f"Premature EOF reading checksum: got {bytes_read}/{remote_size} bytes"
                    )
                md5.update(chunk)
                bytes_read += len(chunk)

        return md5.hexdigest()

    def verify_checksum(self, local_path, expected_checksum):
        try:
            if not os.path.exists(local_path):
                logger.error(f"File not found for checksum: {local_path}")
                return False

            md5 = hashlib.md5()
            chunk_size = self.config["transfer"]["chunk_size"]

            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    md5.update(chunk)

            actual = md5.hexdigest()
            match = actual == expected_checksum
            if not match:
                logger.warning(f"Checksum mismatch: expected {expected_checksum}, got {actual}")
            return match
        except Exception as e:
            logger.error(f"Local checksum failed for {local_path}: {e}")
            return False
