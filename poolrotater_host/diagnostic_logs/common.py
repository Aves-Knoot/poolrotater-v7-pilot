"""Closed bounded wire format and private SQLite helpers."""
import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import zlib

from ..network.contracts import NetworkError, canonical, identifier, require

RAW_LIMIT = 128 * 1024
COMPRESSED_LIMIT = 40 * 1024
RECORD_LIMIT = 128
TEXT_LIMIT = 16 * 1024
ALIAS = re.compile(r'[A-Za-z0-9_.-]{1,80}\Z')
VERSION = re.compile(r'[A-Za-z0-9_.+-]{1,80}\Z')
KINDS = ('operational', 'chat', 'command', 'gap')


def label(value, *, nullable=False):
    require((nullable and value is None) or (isinstance(value, str) and ALIAS.fullmatch(value)), 'invalid_log_label')
    return value


def timestamp(value):
    require(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 2**53 - 1, 'invalid_log_time')
    return value


def private_database(root, name):
    root = Path(root)
    require(root.is_absolute(), 'private_log_root_required')
    # Every caller supplies an application-owned root, never a wire path.
    require(not any(parent.is_symlink() for parent in (root, *root.parents)), 'unsafe_log_root')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = root.lstat()
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077, 'private_log_root_required')
    path = root / name
    if not os.path.lexists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    check_database(path)
    return path


def check_database(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        file = Path(str(path) + suffix)
        if not os.path.lexists(file):
            continue
        st = file.lstat()
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1 and not st.st_mode & 0o077, 'unsafe_log_database')


@contextmanager
def transaction(path):
    check_database(path)
    db = sqlite3.connect(path, timeout=1, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA busy_timeout=1000')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.execute('COMMIT')
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def pack(records):
    raw = canonical(records)
    require(len(raw) <= RAW_LIMIT, 'log_batch_too_large', 413)
    compressed = gzip.compress(raw, mtime=0)
    require(len(compressed) <= COMPRESSED_LIMIT, 'log_batch_too_large', 413)
    return base64.b64encode(compressed).decode('ascii')


def unpack(payload):
    require(type(payload) is str and len(payload) <= (COMPRESSED_LIMIT + 2) // 3 * 4, 'log_batch_too_large', 413)
    try:
        raw = base64.b64decode(payload, validate=True)
        require(len(raw) <= COMPRESSED_LIMIT, 'log_batch_too_large', 413)
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = dec.decompress(raw, RAW_LIMIT + 1)
        require(len(data) <= RAW_LIMIT and dec.eof and not dec.unused_data and not dec.unconsumed_tail, 'invalid_log_compression', 413)
        from ..network.contracts import decode
        records = decode(data, maximum=RAW_LIMIT)
    except (ValueError, zlib.error):
        raise NetworkError('invalid_log_compression') from None
    require(type(records) is list and 1 <= len(records) <= RECORD_LIMIT, 'invalid_log_records')
    previous = -1
    for row in records:
        require(type(row) is dict and set(row) == {'id', 'sequence', 'at', 'source', 'room', 'version', 'kind', 'text', 'epoch', 'start', 'end'}, 'invalid_log_record')
        identifier(row['id']); identifier(row['epoch'])
        require(type(row['sequence']) is int and previous < row['sequence'] <= 2**53 - 1, 'invalid_log_sequence')
        previous = row['sequence']
        timestamp(row['at']); label(row['source']); label(row['room'], nullable=True)
        require(type(row['version']) is str and VERSION.fullmatch(row['version']), 'invalid_log_version')
        require(row['kind'] in KINDS and type(row['text']) is str and len(row['text'].encode()) <= TEXT_LIMIT, 'invalid_log_record')
        require(type(row['start']) is int and type(row['end']) is int and 0 <= row['start'] <= row['end'] <= 2**53 - 1, 'invalid_log_offset')
    return records


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()
