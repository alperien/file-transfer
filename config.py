import copy
import logging
import os
import tempfile
import yaml

logger = logging.getLogger("config")

DEFAULT_CONFIG = {
    "ssh": {
        "host": "192.168.1.100",
        "port": 22,
        "user": "ubuntu",
        "key_path": "~/.ssh/id_rsa",
        "password": "",
    },
    "paths": {
        "source": "/home/ubuntu/files",
        "destination": os.path.expanduser("~/files"),
    },
    "transfer": {
        "chunk_size": 65536,
        "max_retries": 5,
        "retry_delay": 5,
        "timeout": 30,
    },
    "server": {
        "host": "0.0.0.0",
        "port": 5000,
    },
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

SCHEMA = {
    "ssh.port": (int, 1, 65535),
    "server.port": (int, 1, 65535),
    "transfer.chunk_size": (int, 1, None),
    "transfer.max_retries": (int, 0, None),
    "transfer.retry_delay": (int, 0, None),
    "transfer.timeout": (int, 1, None),
}


def _validate_config(config):
    for path, (typ, min_val, max_val) in SCHEMA.items():
        parts = path.split(".")
        value = config
        for part in parts:
            if not isinstance(value, dict):
                break
            value = value.get(part)
        else:
            if not isinstance(value, typ):
                logger.warning(f"Config '{path}' should be {typ.__name__}, got {type(value).__name__}")
                continue
            if min_val is not None and value < min_val:
                logger.warning(f"Config '{path}' must be >= {min_val}, got {value}")
            if max_val is not None and value > max_val:
                logger.warning(f"Config '{path}' must be <= {max_val}, got {value}")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                user_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse config.yaml: {e}")
            user_config = {}
        config = _deep_merge(DEFAULT_CONFIG, user_config)
    else:
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
    _validate_config(config)
    return config


def save_config(config):
    # NOTE: This writes the password to disk. Acceptable for this personal project.
    dir_name = os.path.dirname(CONFIG_PATH)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict):
            if isinstance(value, dict):
                result[key] = _deep_merge(result[key], value)
            else:
                logger.warning(
                    f"Config key '{key}' is a dict in defaults but got {type(value).__name__} "
                    f"in user config; keeping default dict."
                )
        else:
            result[key] = value
    return result
