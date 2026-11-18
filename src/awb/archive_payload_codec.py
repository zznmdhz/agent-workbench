"""Lossless, versioned cold archive-log payload storage.

Normal facts and indexed summaries stay plain JSON. Only archive_log payloads
use this optional representation; export always decodes the original text.
"""
from __future__ import annotations
import struct
import zlib

MAGIC=b'AWBCOLD\x01'
MAX_COMPRESSED_SOURCE=128*1024*1024


def pack(text: str | bytes) -> str | bytes:
    if isinstance(text,bytes):
        unpack(text)
        return text
    if not isinstance(text,str):
        raise TypeError('archive payload must be text or encoded bytes')
    raw=text.encode('utf-8')
    if len(raw)<1024 or len(raw)>MAX_COMPRESSED_SOURCE:
        return text
    compressed=zlib.compress(raw,6)
    encoded=MAGIC+struct.pack('>Q',len(raw))+compressed
    return encoded if len(encoded)<len(raw)*0.9 else text


def unpack(value: str | bytes) -> str:
    if isinstance(value,str):
        return value
    if not isinstance(value,bytes) or not value.startswith(MAGIC) or len(value)<len(MAGIC)+8:
        raise ValueError('unknown archive payload encoding')
    expected=struct.unpack('>Q',value[len(MAGIC):len(MAGIC)+8])[0]
    if expected>MAX_COMPRESSED_SOURCE:
        raise ValueError('archive payload length exceeds codec bound')
    decoder=zlib.decompressobj()
    raw=decoder.decompress(value[len(MAGIC)+8:],expected+1)
    if len(raw)!=expected or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
        raise ValueError('archive payload length/integrity mismatch')
    return raw.decode('utf-8')
