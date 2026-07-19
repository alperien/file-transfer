# file-transfer

A small Flask web UI for downloading files from a remote server over SFTP
(paramiko), with a persistent queue (SQLite), resume support, retries,
checksum verification, and live progress.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.yaml.example config.yaml
# edit config.yaml: ssh host/user/key or password, source and destination paths
```

## Run

```bash
python server.py
```

Then open http://127.0.0.1:5000

## Security notes

- `config.yaml` contains SSH credentials. It is **git-ignored** — never commit
  it. Use `config.yaml.example` as a template.
- The web UI has **no authentication or CSRF protection**. Keep
  `server.host: 127.0.0.1` (the default) so it is only reachable from your own
  machine. Do not expose it to the internet.
- Prefer SSH key authentication (`key_path`) over a password.
