#!/usr/bin/env python3
"""Git credential protocol helper restricted to explicitly configured ADO repos."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import unquote, urlsplit


def private_read(path, limit=65536):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('private path must be absolute')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('untrusted private file')
        raw = os.read(fd, limit + 1)
        if len(raw) > limit: raise ValueError('private file exceeds bound')
        return raw.decode()
    finally:
        os.close(fd)


def repository_path(value):
    decoded = unquote(value)
    if any(ord(c) < 32 or c in '\\?#%' for c in decoded): return None
    parts = decoded.split('/')
    if len(parts) != 4 or parts[2] != '_git' or any(p in ('', '.', '..') for p in parts): return None
    return decoded


def credentials_for_request(fields, config_path):
    try:
        if fields.get('protocol') != 'https' or fields.get('host') != 'dev.azure.com': return {}
        path = repository_path(fields.get('path', ''))
        if path is None: return {}
        config = json.loads(private_read(config_path))
        matches = []
        for row in config['repositories']:
            url = urlsplit(row['url'])
            if (url.scheme == 'https' and url.netloc == 'dev.azure.com' and not url.query and not url.fragment
                    and repository_path(url.path.removeprefix('/')) == path): matches.append(row)
        if len(matches) != 1: return {}
        secret = private_read(matches[0]['credential_file'], 8192).strip()
        if not secret or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in secret): return {}
        return {'username': 'pursers', 'password': secret}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('operation', choices=('get', 'store', 'erase'))
    args = parser.parse_args()
    if args.operation != 'get': return 0
    # Git reads the credential protocol from an anonymous pipe. Refuse a
    # terminal or regular log file, including accidental service execution.
    if not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode): return 0
    raw = sys.stdin.read(16385)
    if len(raw) > 16384: return 0
    fields = {}
    for line in raw.splitlines():
        if not line: break
        if '=' not in line: return 0
        key, value = line.split('=', 1)
        if key in fields: return 0
        fields[key] = value
    result = credentials_for_request(fields, args.config)
    # This is a required Git credential-protocol response, never a log message.
    if result: sys.stdout.write(''.join(f'{k}={v}\n' for k,v in result.items()) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
