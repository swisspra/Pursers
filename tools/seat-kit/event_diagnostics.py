"""Bounded owner-private event-runner diagnostics; never capture model stdout."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time


MAX_CAPTURE_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 20 * 1024
MAX_FILE_BYTES = 32 * 1024
MAX_FILES = 20
RETENTION_S = 7 * 24 * 60 * 60
_FILE = re.compile(r'^diag-[A-Za-z0-9._-]{1,150}\.json$')


class BoundedStderr:
    """Drain child stderr without blocking it, retaining only a bounded tail."""
    def __init__(self, limit=MAX_CAPTURE_BYTES):
        self.limit = limit
        self._parts = []
        self._lock = threading.Lock()
        self._size = 0
        self._total_bytes = 0
        self.read_error = None

    @property
    def total_bytes(self):
        with self._lock:
            return self._total_bytes

    def drain(self, stream):
        try:
            while True:
                # read1 returns currently available pipe bytes instead of
                # waiting to fill the whole requested buffer while the child
                # is still running.
                reader = getattr(stream, 'read1', None) or stream.read
                chunk = reader(8192)
                if not chunk:
                    break
                with self._lock:
                    self._total_bytes += len(chunk)
                    if len(chunk) >= self.limit:
                        self._parts = [chunk[-self.limit:]]
                        self._size = self.limit
                    else:
                        self._parts.append(chunk)
                        self._size += len(chunk)
                        overflow = self._size - self.limit
                        while overflow > 0:
                            if len(self._parts[0]) <= overflow:
                                overflow -= len(self._parts.pop(0))
                            else:
                                self._parts[0] = self._parts[0][overflow:]
                                overflow = 0
                        self._size = sum(map(len, self._parts))
        except (OSError, ValueError) as exc:
            self.read_error = type(exc).__name__
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @property
    def truncated(self):
        with self._lock:
            return self._total_bytes > self._size

    def text(self):
        with self._lock:
            return b''.join(self._parts).decode('utf-8', errors='replace')


def redact(text, secrets=()):
    """Remove known values and common credential/header formats."""
    for value in sorted({v for v in secrets if isinstance(v, str) and len(v) >= 6},
                        key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    patterns = (
        (r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}', r'\1 [REDACTED]'),
        (r'(?im)\b((?:x-)?(?:api[-_]key|access[-_]token|refresh[-_]token|client[-_]secret|subscription[-_]key)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]'),
        (r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;]+', r'\1[REDACTED]'),
        (r'(?i)\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b', '[REDACTED_JWT]'),
        (r'(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)\s*[:=]\s*[\"\']?[^\s,;\"\']+', r'\1=[REDACTED]'),
        (r'(?i)(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@'),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _secure_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('diagnostics path must be absolute')
    parent_info = path.parent.lstat()
    if (path.parent.is_symlink() or path.parent.resolve() != path.parent
            or not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o077):
        raise ValueError('diagnostics parent is untrusted')
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=False)
        info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('diagnostics directory is untrusted')
    return path


def classify_failure(stderr):
    """Classify only explicit provider/transport evidence; never authorize a retry."""
    text = (stderr or '').casefold()
    if any(marker in text for marker in (
        'http 401', 'http 403', '401 unauthorized', '403 forbidden',
        'authentication failed', 'invalid api key', 'invalid access token',
    )):
        return 'authentication_failure'
    if any(marker in text for marker in (
        'provider unavailable', 'provider is unavailable', 'no healthy provider',
        'model unavailable', 'service unavailable from provider', 'provider health check failed',
    )):
        return 'provider_unavailable'
    if any(marker in text for marker in (
        'connection refused', 'connection reset', 'server disconnected', 'connection closed',
        'transport error', 'transport closed', 'read timed out', 'connect timeout',
        'network is unreachable', 'tls handshake', 'remoteprotocolerror', 'broken pipe',
    )):
        return 'transport_failure'
    return None


def _retire(directory, now):
    entries = []
    for path in directory.iterdir():
        if not _FILE.fullmatch(path.name):
            raise ValueError('diagnostics directory contains an unknown entry')
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_FILE_BYTES):
            raise ValueError('diagnostic evidence file is untrusted')
        entries.append((info.st_mtime, path))
    entries.sort()
    retained = []
    for mtime, path in entries:
        if now - mtime > RETENTION_S:
            path.unlink()
        else:
            retained.append((mtime, path))
    entries = retained
    while len(entries) >= MAX_FILES:
        _, path = entries.pop(0)
        path.unlink()


def write_evidence(state_path, attempt_id, record, stderr, secrets=(),
                   stderr_truncated=False, stdout_captured=False, prompt_redacted=True):
    """Atomically retain one strictly bounded, owner-only evidence record."""
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,120}', attempt_id):
        raise ValueError('diagnostic attempt id is invalid')
    parent = Path(state_path).parent
    directory = _secure_directory(parent / 'diagnostics')
    now = time.time()
    _retire(directory, now)
    cleaned = redact(stderr, secrets)
    encoded = cleaned.encode('utf-8', errors='replace')
    omissions = {
        'stdout': 'not_captured' if not stdout_captured else 'captured',
        'private_prompt': 'not_captured',
        'stderr_truncated': bool(stderr_truncated or len(encoded) > MAX_EVIDENCE_BYTES),
    }
    if len(encoded) > MAX_EVIDENCE_BYTES:
        encoded = encoded[-MAX_EVIDENCE_BYTES:]
        cleaned = encoded.decode('utf-8', errors='replace')
    document = dict(record)
    document.update(schema='pursers_event_diagnostic_v1', stderr=cleaned,
                    omissions=omissions, recorded_at=now)
    data = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    while len(data) > MAX_FILE_BYTES and cleaned:
        cleaned = cleaned[len(cleaned) // 4:]
        document['stderr'] = cleaned
        document['omissions']['stderr_truncated'] = True
        data = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('diagnostic record exceeds safe bound')
    target = directory / f'diag-{attempt_id}.json'
    try:
        old = target.lstat()
    except FileNotFoundError:
        old = None
    if old is not None and (target.is_symlink() or not stat.S_ISREG(old.st_mode)
                            or old.st_uid != os.getuid() or old.st_nlink != 1
                            or stat.S_IMODE(old.st_mode) != 0o600):
        raise ValueError('diagnostic target is untrusted')
    fd, temporary = tempfile.mkstemp(prefix='.diag-', dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
