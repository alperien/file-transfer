# Technical Plan: Ubuntu-to-Windows File Transfer App

## Overview
A simple, bulletproof Python application that pulls files from an Ubuntu server to local Windows HDD via SSH/SCP. Web UI for manual control. Auto-resumes on connection drops. Files never corrupt.

---

## Core Design Principles (KISS)

1. **One file = one purpose** - Easy to debug
2. **No magic** - Every operation is logged
3. **Fail-safe defaults** - If unsure, don't transfer
4. **Checksums always** - Verify every file before marking complete
5. **Atomic operations** - Transfer to `.tmp` first, then rename

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Windows Client                        │
├─────────────────────────────────────────────────────────┤
│  Web UI (Browser)  ◄──HTTP──►  Flask Server (Python)    │
│                                    │                    │
│                                    ▼                    │
│                           Transfer Engine               │
│                           ├─ File Queue                 │
│                           ├─ Progress Tracker           │
│                           └─ Checksum Verifier          │
│                                    │                    │
│                                    ▼                    │
│                           SSH/SCP Connection            │
│                           (paramiko library)            │
└─────────────────────────────────────────────────────────┘
                         │
                    SSH Connection
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   Ubuntu Server                          │
│  /path/to/source/files/*                                 │
└─────────────────────────────────────────────────────────┘
```

---

## File Structure

```
file-transfer/
├── server.py              # Main entry point (Flask app)
├── transfer_engine.py     # Core transfer logic
├── ssh_client.py          # SSH/SCP connection wrapper
├── config.py              # Configuration (host, paths, etc.)
├── db.py                  # SQLite for tracking transfers
├── templates/
│   └── index.html         # Web UI
├── static/
│   └── app.js             # Frontend JavaScript
├── requirements.txt       # Dependencies
└── README.md              # Setup instructions
```

---

## Component Details

### 1. config.py - Configuration
```python
# Simple config file, no magic
SSH_HOST = "your-ubuntu-ip"
SSH_PORT = 22
SSH_USER = "your-username"
SSH_KEY_PATH = "~/.ssh/id_rsa"  # Or use password
SOURCE_DIR = "/home/user/files"  # Ubuntu path
DEST_DIR = "C:\\Users\\you\\files"  # Windows path
CHUNK_SIZE = 8192  # 8KB chunks - small but reliable
```

### 2. ssh_client.py - SSH Connection Wrapper
**Responsibilities:**
- Connect/disconnect cleanly
- List remote files with metadata (size, mtime)
- Download single file with progress callback
- Calculate remote checksum (md5/sha256)
- Handle connection errors gracefully

**Key Methods:**
```python
class SSHClient:
    def connect(host, port, user, key_path)
    def disconnect()
    def list_files(remote_path) -> List[FileInfo]
    def download_file(remote_path, local_path, progress_callback)
    def get_checksum(remote_path) -> str
    def is_connected() -> bool
    def reconnect()  # Auto-reconnect logic
```

**Bulletproof Features:**
- Keep-alive pings every 30 seconds
- Auto-reconnect on connection loss (exponential backoff)
- Timeout on all operations (prevent hanging)
- Log every SSH operation

### 3. transfer_engine.py - Core Transfer Logic
**Responsibilities:**
- Manage file queue
- Track progress per file
- Handle resume (track bytes transferred)
- Verify checksums after transfer
- Retry failed transfers

**Key Methods:**
```python
class TransferEngine:
    def add_to_queue(files: List[FileInfo])
    def start_transfer()
    def pause_transfer()
    def resume_transfer()
    def get_progress() -> Dict[file, Progress]
    def verify_file(local_path, expected_checksum) -> bool
```

**Bulletproof Features:**
1. **Atomic Writes:** Download to `file.tmp`, rename on success
2. **Progress Tracking:** Store bytes transferred in SQLite
3. **Resume Logic:** If file exists and matches size, skip or resume
4. **Checksum Verification:** MD5 after every transfer
5. **Retry Logic:** 3 attempts with exponential backoff
6. **No Partial Files:** Delete `.tmp` on failure

**Transfer Flow:**
```
1. Check if file exists locally
   - If exists AND size matches remote → Skip (already done)
   - If exists AND size mismatch → Delete and re-transfer
2. Open remote file for reading
3. Open local `.tmp` file for writing
4. Read chunk from remote
5. Write chunk to local
6. Update progress in DB
7. Repeat 4-6 until done
8. Verify checksum matches
9. If checksum OK → Rename .tmp to final name
10. If checksum FAIL → Delete .tmp, retry from step 1
```

### 4. db.py - SQLite Tracking
**Tables:**
```sql
-- Track all files we know about
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    remote_path TEXT UNIQUE,
    local_path TEXT,
    size INTEGER,
    checksum TEXT,
    status TEXT  -- 'pending', 'transferring', 'complete', 'failed'
);

-- Track transfer progress for resume
CREATE TABLE progress (
    file_id INTEGER PRIMARY KEY,
    bytes_transferred INTEGER,
    last_updated TIMESTAMP,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

-- Log all operations for debugging
CREATE TABLE logs (
    id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP,
    level TEXT,  -- 'INFO', 'WARNING', 'ERROR'
    message TEXT
);
```

### 5. server.py - Flask Web Server
**Endpoints:**
```
GET  /                    → Web UI
GET  /api/connect         → Connect to Ubuntu server
POST /api/disconnect      → Disconnect
GET  /api/files           → List remote files
POST /api/transfer        → Add files to queue
POST /api/pause           → Pause transfer
POST /api/resume          → Resume transfer
GET  /api/progress        → Get transfer progress
GET  /api/logs            → Get recent logs
```

**Features:**
- Single-threaded transfer (simplicity)
- Non-blocking operations (background thread)
- WebSocket or polling for progress updates

### 6. templates/index.html - Web UI
**Layout:**
```
┌─────────────────────────────────────────────────────────┐
│  SSH Connection: [Host] [User] [Key] [Connect Button]   │
├─────────────────────────────────────────────────────────┤
│  Remote Files (Ubuntu)      │  Local Destination        │
│  ┌─────────────────────┐   │  ┌─────────────────────┐  │
│  │ ☑ folder1/          │   │  │ C:\Users\you\files  │  │
│  │ ☑ file1.pdf (2.3MB)│   │  └─────────────────────┘  │
│  │ ☐ file2.jpg (1.1MB)│   │                            │
│  │ ☑ video.mp4 (500MB)│   │  [Browse Folder]           │
│  └─────────────────────┘   │                            │
│                            │                            │
│  [Add to Queue]            │                            │
├─────────────────────────────────────────────────────────┤
│  Transfer Queue                                      [▶] │
│  ┌─────────────────────────────────────────────────────┐│
│  │ video.mp4    ████████░░░░░░░░░  45%  225MB/500MB  ││
│  │ file1.pdf    ░░░░░░░░░░░░░░░░░  0%   Pending      ││
│  │ folder1/     ────────────────  Complete (3 files)  ││
│  └─────────────────────────────────────────────────────┘│
│                                                         │
│  Status: Transferring... | Speed: 10.2 MB/s | ETA: 2m  │
├─────────────────────────────────────────────────────────┤
│  Logs                                                   │
│  ┌─────────────────────────────────────────────────────┐│
│  │ [12:34:56] INFO: Connected to ubuntu-server         ││
│  │ [12:34:57] INFO: Found 150 files (2.3 GB)         ││
│  │ [12:34:58] INFO: Starting transfer of video.mp4    ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

---

## Error Handling Strategy

### Connection Drops
```python
def download_with_resume(remote_path, local_path):
    for attempt in range(MAX_RETRIES):
        try:
            # Check if we have partial download
            local_size = os.path.getsize(local_path + ".tmp") if os.path.exists(local_path + ".tmp") else 0
            
            # Open remote file with offset
            with sftp.open(remote_path, 'rb') as remote_file:
                remote_file.seek(local_size)
                
                with open(local_path + ".tmp", 'ab') as local_file:
                    while True:
                        chunk = remote_file.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        local_file.write(chunk)
                        # Update progress in DB
            
            # Verify checksum
            if verify_checksum(local_path + ".tmp", expected_checksum):
                os.rename(local_path + ".tmp", local_path)
                return True
            else:
                os.remove(local_path + ".tmp")
                
        except ConnectionError:
            wait_with_backoff(attempt)
            reconnect()
    
    return False  # Failed after all retries
```

### File Corruption Prevention
1. **Never write directly to final file** - Always use `.tmp`
2. **Always verify checksum** - MD5 after transfer
3. **Log everything** - Know exactly what happened
4. **Atomic rename** - Only rename when 100% verified

### Logging
```python
# Every operation gets logged
logger.info(f"Starting transfer: {remote_path} -> {local_path}")
logger.info(f"Chunk {n}: {bytes_transferred} bytes")
logger.warning(f"Connection lost, reconnecting... (attempt {attempt})")
logger.error(f"Checksum mismatch: expected {expected}, got {actual}")
```

---

## Dependencies

```txt
# requirements.txt
flask==3.0.0
paramiko==3.4.0
watchdog==3.0.0  # Optional: for file watching
```

**Why these choices:**
- `flask` - Simple, well-documented, one file
- `paramiko` - Battle-tested SSH library
- `watchdog` - Only if we add file watching later

---

## Configuration File

```yaml
# config.yaml - Simple and readable
ssh:
  host: "192.168.1.100"
  port: 22
  user: "ubuntu"
  key_path: "~/.ssh/id_rsa"

paths:
  source: "/home/ubuntu/files"
  destination: "C:\\Users\\you\\files"

transfer:
  chunk_size: 8192
  max_retries: 5
  retry_delay: 5  # seconds
  timeout: 30  # seconds

server:
  host: "0.0.0.0"
  port: 5000
```

---

## Implementation Steps

### Phase 1: Core SSH (30 min)
1. Set up `config.py` with simple config
2. Implement `ssh_client.py` with connect/list/download
3. Test SSH connection manually

### Phase 2: Transfer Engine (45 min)
1. Create SQLite schema in `db.py`
2. Implement `transfer_engine.py` with queue and progress
3. Add checksum verification
4. Add resume logic

### Phase 3: Web Server (30 min)
1. Set up Flask app in `server.py`
2. Create basic HTML template
3. Add API endpoints
4. Connect frontend to backend

### Phase 4: Web UI (30 min)
1. Build file browser interface
2. Add transfer queue display
3. Add progress bars
4. Add log viewer

### Phase 5: Testing (15 min)
1. Test with small files
2. Test with large files
3. Test connection drops (unplug network)
4. Test resume after drop
5. Test checksum verification

---

## Testing Checklist

- [ ] Connect to Ubuntu via SSH
- [ ] List remote files correctly
- [ ] Transfer small file (< 1MB)
- [ ] Transfer large file (> 100MB)
- [ ] Resume interrupted transfer
- [ ] Verify checksum matches
- [ ] Handle connection drop mid-transfer
- [ ] Auto-reconnect after drop
- [ ] No partial files left on failure
- [ ] Logs show all operations
- [ ] Web UI updates in real-time
- [ ] Multiple files in queue
- [ ] Pause/resume queue
- [ ] Error messages are clear

---

## Success Criteria

1. **Files never corrupt** - Checksums always match
2. **Auto-resume works** - Connection drops don't lose progress
3. **Simple to debug** - Logs tell you exactly what happened
4. **KISS** - No complex abstractions, just working code
5. **Web UI works** - Can control everything from browser

---

## Future Enhancements (NOT in v1)

- Parallel transfers (multiple files at once)
- File watching (auto-transfer new files)
- Compression (save bandwidth)
- Encryption at rest
- Cloud storage integration
