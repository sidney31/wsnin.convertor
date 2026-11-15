"""Fix for access_parser: correct decoding of Access "compressed unicode" text.

Access 2000+ stores Text/Memo values that start with the 0xFF 0xFE marker in a
compressed form: characters are 1 byte (high byte = 0) until a 0x00 byte
switches to plain UTF-16LE (2 bytes per character); the next 0x00 switches back.
access_parser decodes such values as latin-1, so Cyrillic turns into garbage.
"""
from access_parser import access_parser as _ap
from access_parser import utils as _utils

_original_parse_type = _utils.parse_type


def decode_compressed_unicode(data: bytes) -> str:
    out = []
    compressed = True
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0:  # mode switch
            compressed = not compressed
            i += 1
            continue
        if compressed:
            out.append(chr(b))
            i += 1
        else:
            if i + 1 >= n:
                break
            out.append(chr(b | (data[i + 1] << 8)))
            i += 2
    return "".join(out)


def _parse_type(data_type, buffer, length=None, version=3, props=None):
    if (
        data_type == _utils.TYPE_TEXT
        and version > 3
        and isinstance(buffer, (bytes, bytearray))
        and buffer[:2] == b"\xff\xfe"
    ):
        return decode_compressed_unicode(bytes(buffer[2:]))
    return _original_parse_type(data_type, buffer, length, version=version, props=props)


def install() -> None:
    """Patch access_parser so every table read uses the correct decoder."""
    _utils.parse_type = _parse_type
    _ap.parse_type = _parse_type
