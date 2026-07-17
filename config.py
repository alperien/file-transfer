import copy
import os
import tempfile
import yaml

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


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                user_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            import logging
            logging.getLogger("config").error(f"Failed to parse config.yaml: {e}")
            user_config = {}
        config = _deep_merge(DEFAULT_CONFIG, user_config)
    else:
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
    return config


def save_config(config):
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
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
