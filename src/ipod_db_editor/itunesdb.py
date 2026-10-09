"""Byte-exact parse and render of the iTunesDB chunk tree and its text records."""
import struct


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def put32(data, offset, value):
    struct.pack_into('<I', data, offset, value)


class Chunk:
    def __init__(self, raw):
        if len(raw) < 12:
            raise ValueError('Truncated chunk')
        self.raw = bytes(raw)
        self.tag = raw[:4]
        self.hlen = u32(raw, 4)
        if self.hlen < 12 or self.hlen > len(raw):
            raise ValueError('Invalid chunk header length')
        if self.tag != b'mhlt' and u32(raw, 8) != len(raw):
            raise ValueError('Chunk length mismatch')
        self.header = bytearray(raw[:self.hlen])
        self.body = raw[self.hlen:]
        self.children = None
        if self.tag == b'mhbd':
            self.children = split(self.body, u32(raw, 20))
        elif self.tag == b'mhsd' and u32(raw, 12) == 1:
            if self.body[:4] != b'mhlt':
                raise ValueError('Track dataset has no mhlt')
            self.children = [Chunk(self.body)]
        elif self.tag == b'mhlt':
            self.children = split(self.body, u32(raw, 8))
            if any(c.tag != b'mhit' for c in self.children):
                raise ValueError('Unexpected track-list child')
        elif self.tag == b'mhit':
            self.children = split(self.body, u32(raw, 12))
            if self.hlen < 0xD4 or any(c.tag != b'mhod' for c in self.children):
                raise ValueError('Unsupported track layout')

    def render(self):
        body = self.body if self.children is None else b''.join(c.render() for c in self.children)
        header = bytearray(self.header)
        if self.tag != b'mhlt':
            put32(header, 8, len(header) + len(body))
        if self.tag == b'mhit':
            put32(header, 12, len(self.children))
        return bytes(header) + body


def split(body, count):
    if count > len(body) // 12:
        raise ValueError('Invalid child count')
    result, offset = [], 0
    for _ in range(count):
        if offset + 12 > len(body):
            raise ValueError('Truncated child record')
        size = u32(body, offset + 8)
        if size < 12 or offset + size > len(body):
            raise ValueError('Invalid child length')
        result.append(Chunk(body[offset:offset + size]))
        offset += size
    if offset != len(body):
        raise ValueError('Unparsed bytes in container; refusing to discard them')
    return result


def parse(data):
    if len(data) < 244 or data[:4] != b'mhbd':
        raise ValueError('Expected an uncompressed Classic iTunesDB')
    if u32(data, 12) != 1 or u32(data, 16) != 0x75:
        raise ValueError('This version supports the examined database version 0x75 only')
    root = Chunk(data)
    datasets = [c for c in root.children if c.tag == b'mhsd' and u32(c.header, 12) == 1]
    if len(datasets) != 1:
        raise ValueError('Expected exactly one track dataset')
    tracks = datasets[0].children[0].children
    ids = [persistent_id(t) for t in tracks]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate persistent IDs')
    if root.render() != data:
        raise ValueError('Unmodified parse did not preserve every byte')
    return root, tracks


def persistent_id(track):
    return '%016X' % struct.unpack_from('<Q', track.header, 0x70)[0]


def text_of(c):
    if c.hlen != 24 or len(c.body) < 16:
        raise ValueError('Unsupported text record layout')
    encoding = {1: 'utf-16-le', 2: 'utf-8'}.get(u32(c.body, 0))
    size = u32(c.body, 4)
    if not encoding or size > len(c.body) - 16:
        raise ValueError('Unsupported or truncated text encoding')
    return c.body[16:16 + size].decode(encoding)


def get_text(track, kind):
    matches = [c for c in track.children if u32(c.header, 12) == kind]
    if len(matches) > 1:
        raise ValueError('Duplicate text records; unsupported')
    if not matches:
        return '', None
    return text_of(matches[0]), matches[0]


def set_text(track, kind, value):
    old, c = get_text(track, kind)
    if old == value:
        return old
    if '\0' in value:
        raise ValueError('Text cannot contain NUL characters')
    if c is None:
        # New text uses Apple's standard UTF-16 record layout.
        payload = value.encode('utf-16-le')
        body = struct.pack('<IIII', 1, len(payload), 1, 0) + payload
        c = Chunk(struct.pack('<4sIIIII', b'mhod', 24, 24 + len(body), kind, 0, 0) + body)
        track.children.append(c)
    else:
        size = u32(c.body, 4)
        payload = value.encode({1: 'utf-16-le', 2: 'utf-8'}[u32(c.body, 0)])
        prefix = bytearray(c.body[:16])
        put32(prefix, 4, len(payload))
        c.body = bytes(prefix) + payload + c.body[16 + size:]
    return old
