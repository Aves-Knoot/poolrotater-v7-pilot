"""Lossless indefinite diagnostics: gzip level 4 inside the private SQLite store.

Only closed observations older than three days are compressed. IDs, timestamps,
redaction, receipts and search metadata stay intact. No row is removed. A short
transaction rechecks the exact original bytes before replacing its text column.
SQLite's TEXT column deliberately accepts a BLOB for compressed old records;
all official reads decode it transparently. This additive representation requires
an archive-aware hub for rollback after the first compression run.
"""
import gzip
import time
import zlib
from .common import TEXT_LIMIT, check_database, transaction
from ..network.contracts import require

AGE_SECONDS = 3 * 86400
LEVEL = 4
MAX_BYTES = TEXT_LIMIT * 4


def decode(value):
    if isinstance(value, str):
        return value
    require(isinstance(value, bytes), 'invalid_archived_log')
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(value, MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES and decoder.eof and not decoder.unused_data
                and not decoder.unconsumed_tail, 'invalid_archived_log')
        return raw.decode('utf-8')
    except (zlib.error, UnicodeError):
        raise ValueError('invalid_archived_log') from None


def compress_old(path, *, now=None, batch_size=128, maximum=4096):
    """Bounded recurring work. Active ingestion can continue between batches."""
    import sqlite3
    check_database(path)
    require(type(batch_size) is int and 1 <= batch_size <= 512, 'invalid_archive_batch')
    require(type(maximum) is int and 1 <= maximum <= 65536, 'invalid_archive_batch')
    cutoff = (time.time() if now is None else now) - AGE_SECONDS
    count = saved = 0
    while count < maximum:
        with sqlite3.connect(path, timeout=1) as db:
            rows = db.execute("SELECT sequence,text FROM logs WHERE received<? AND typeof(text)='text' ORDER BY sequence LIMIT ?",
                              (cutoff, min(batch_size, maximum-count))).fetchall()
        if not rows:
            break
        prepared = []
        for sequence, text in rows:
            raw = text.encode('utf-8')
            require(len(raw) <= MAX_BYTES, 'invalid_archived_log')
            compressed = gzip.compress(raw, compresslevel=LEVEL, mtime=0)
            require(decode(compressed) == text, 'archive_roundtrip_failed')
            prepared.append((sequence, text, compressed))
        with transaction(path) as db:
            for sequence, text, compressed in prepared:
                changed = db.execute("UPDATE logs SET text=? WHERE sequence=? AND typeof(text)='text' AND text=? AND received<?",
                                     (compressed, sequence, text, cutoff)).rowcount
                count += changed
                saved += changed * (len(text.encode('utf-8'))-len(compressed))
        # An opt-out may have deleted all prepared rows. Never loop on a race.
        if not prepared:
            break
    return {'ok': True, 'compressed_records': count, 'text_bytes_saved': saved,
            'compression': 'gzip', 'level': LEVEL, 'older_than_seconds': AGE_SECONDS,
            'retention_seconds': None, 'records_deleted': 0}
