import logging
import logging.handlers
import os
import platform
import threading

from flask import Flask, jsonify, render_template, request

import db
from config import load_config, save_config
from transfer_engine import TransferEngine

if platform.system() == "Windows":
    import ntpath
    pathmod = ntpath
else:
    import posixpath
    pathmod = posixpath

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "transfer.log", maxBytes=5_000_000, backupCount=3
        ),
    ],
)

logger = logging.getLogger("server")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

config = load_config()
engine = TransferEngine(config)
_config_lock = threading.Lock()


def _get_json():
    if not request.is_json:
        return None
    return request.get_json(silent=True)


def _validate_config_types(new_config):
    errors = []
    if "ssh" in new_config:
        ssh = new_config["ssh"]
        if "host" in ssh and not isinstance(ssh["host"], str):
            errors.append("ssh.host must be a string")
        if "port" in ssh:
            if not isinstance(ssh["port"], int) or not (1 <= ssh["port"] <= 65535):
                errors.append("ssh.port must be an integer 1-65535")
        if "user" in ssh and not isinstance(ssh["user"], str):
            errors.append("ssh.user must be a string")
        if "key_path" in ssh and not isinstance(ssh["key_path"], str):
            errors.append("ssh.key_path must be a string")
        if "password" in ssh and not isinstance(ssh["password"], str):
            errors.append("ssh.password must be a string")
    if "paths" in new_config:
        paths = new_config["paths"]
        if "source" in paths and not isinstance(paths["source"], str):
            errors.append("paths.source must be a string")
        if "destination" in paths and not isinstance(paths["destination"], str):
            errors.append("paths.destination must be a string")
    if "transfer" in new_config:
        t = new_config["transfer"]
        if "chunk_size" in t:
            if not isinstance(t["chunk_size"], int) or t["chunk_size"] <= 0:
                errors.append("transfer.chunk_size must be a positive integer")
        if "max_retries" in t:
            if not isinstance(t["max_retries"], int) or t["max_retries"] < 1:
                errors.append("transfer.max_retries must be an integer >= 1")
        if "retry_delay" in t:
            if not isinstance(t["retry_delay"], (int, float)) or t["retry_delay"] < 0:
                errors.append("transfer.retry_delay must be a non-negative number")
        if "timeout" in t:
            if not isinstance(t["timeout"], (int, float)) or t["timeout"] < 1:
                errors.append("transfer.timeout must be a positive number")
    if "server" in new_config:
        s = new_config["server"]
        if "host" in s and not isinstance(s["host"], str):
            errors.append("server.host must be a string")
        if "port" in s:
            if not isinstance(s["port"], int) or not (1 <= s["port"] <= 65535):
                errors.append("server.port must be an integer 1-65535")
    return errors


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET"])
def get_config():
    with _config_lock:
        safe_config = {
            "ssh": {
                "host": config["ssh"]["host"],
                "port": config["ssh"]["port"],
                "user": config["ssh"]["user"],
                "key_path": config["ssh"]["key_path"],
                "password": "***" if config["ssh"].get("password") else "",
            },
            "paths": dict(config["paths"]),
            "transfer": dict(config["transfer"]),
            "server": dict(config["server"]),
        }
    return jsonify(safe_config)


@app.route("/api/config", methods=["POST"])
def update_config():
    new_config = _get_json()
    if not new_config:
        return jsonify({"ok": False, "message": "Invalid JSON"}), 400

    errors = _validate_config_types(new_config)
    if errors:
        return jsonify({"ok": False, "message": "; ".join(errors)}), 400

    allowed_ssh_keys = {"host", "port", "user", "key_path", "password"}
    allowed_path_keys = {"source", "destination"}
    allowed_transfer_keys = {"chunk_size", "max_retries", "retry_delay", "timeout"}
    allowed_server_keys = {"host", "port"}

    with _config_lock:
        if "ssh" in new_config:
            for key in new_config["ssh"]:
                if key in allowed_ssh_keys:
                    config["ssh"][key] = new_config["ssh"][key]
        if "paths" in new_config:
            for key in new_config["paths"]:
                if key in allowed_path_keys:
                    config["paths"][key] = new_config["paths"][key]
        if "transfer" in new_config:
            for key in new_config["transfer"]:
                if key in allowed_transfer_keys:
                    config["transfer"][key] = new_config["transfer"][key]
        if "server" in new_config:
            for key in new_config["server"]:
                if key in allowed_server_keys:
                    config["server"][key] = new_config["server"][key]

        save_config(config)
    return jsonify({"ok": True})


@app.route("/api/connect", methods=["POST"])
def connect():
    try:
        engine.connect()
        return jsonify({"ok": True, "message": "Connected"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    try:
        engine.disconnect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/files")
def list_files():
    with _config_lock:
        source = config["paths"]["source"]
    path = request.args.get("path", source)

    norm_source = pathmod.normpath(source)
    norm_path = pathmod.normpath(path)

    if norm_path != norm_source:
        has_sep = norm_path.startswith(norm_source + pathmod.sep) or norm_path.startswith(norm_source + "/")
        if not has_sep:
            return jsonify({"ok": False, "message": "Access denied: path outside source directory"}), 403

    try:
        files = engine.list_remote_files(path)
        return jsonify({"ok": True, "files": files})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/queue", methods=["POST"])
def queue_files():
    data = _get_json()
    if not data:
        return jsonify({"ok": False, "message": "Invalid JSON"}), 400

    files = data.get("files", [])
    if not isinstance(files, list):
        return jsonify({"ok": False, "message": "files must be a list"}), 400

    valid_files = [f for f in files if isinstance(f, dict) and "path" in f]

    try:
        count = engine.add_files_to_queue(valid_files)
        return jsonify({"ok": True, "added": count})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/transfer/start", methods=["POST"])
def start_transfer():
    with engine._lock:
        try:
            if not engine.is_connected():
                engine.connect()
            engine.start()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/transfer/pause", methods=["POST"])
def pause_transfer():
    try:
        engine.pause()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/transfer/resume", methods=["POST"])
def resume_transfer():
    try:
        engine.resume()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/transfer/stop", methods=["POST"])
def stop_transfer():
    try:
        engine.stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/transfer/status")
def transfer_status():
    return jsonify(engine.get_status())


@app.route("/api/queue/files")
def queue_files_list():
    try:
        files = db.get_all_files()
        return jsonify(files)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/queue/clear", methods=["POST"])
def clear_queue():
    try:
        db.clear_completed()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/logs")
def get_logs():
    limit = request.args.get("limit", 100, type=int)
    try:
        logs = db.get_logs(limit)
        return jsonify(logs)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


if __name__ == "__main__":
    logger.info("Starting file transfer server")
    app.run(
        host=config["server"]["host"],
        port=config["server"]["port"],
        debug=False,
    )
