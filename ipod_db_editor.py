#!/usr/bin/env python3
"""Patch track fields without rebuilding Apple's database. Python 3.9+ / macOS."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

TEXT = {
    'title': 1, 'album': 3, 'artist': 4, 'genre': 5, 'comment': 8,
    'category': 9, 'lyrics': 10, 'composer': 12, 'grouping': 13,
    'description': 14, 'subtitle': 18, 'show': 19, 'episode': 20,
    'tv_network': 21, 'album_artist': 22, 'sort_artist': 23,
    'keywords': 24, 'sort_title': 27, 'sort_album': 28,
    'sort_album_artist': 29, 'sort_composer': 30, 'sort_show': 31,
}
# name: byte offset, struct format, minimum, maximum
NUMBER = {
    'rating': (0x1F, 'B', 0, 100),
    'track_number': (0x2C, 'I', 0, 65535),
    'total_tracks': (0x30, 'I', 0, 65535),
    'year': (0x34, 'I', 0, 9999),
    'disc_number': (0x5C, 'I', 0, 65535),
    'total_discs': (0x60, 'I', 0, 65535),
    'bpm': (0x7A, 'H', 0, 65535),
    'skip_when_shuffling': (0xA5, 'B', 0, 1),
    'remember_position': (0xA6, 'B', 0, 1),
    'media_type': (0xD0, 'I', 0, 0xFFFFFFFF),
}
MEDIA_TYPES = {
    'music': 0x01, 'video': 0x02, 'podcast': 0x04, 'audiobook': 0x08,
    'music_video': 0x20, 'tv_show': 0x40,
}
# These edits can affect indexes, grouping, or other records kept by Apple.
EXPERIMENTAL = set(TEXT) | {'track_number', 'disc_number', 'media_type', 'podcast_group'}
AES_KEY = '618ca10dc7f57fd3b4723e08157463d7'


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


def record(body, offset, tag):
    # Return (header length, total length) of the record at offset after a tag check.
    if offset + 12 > len(body) or body[offset:offset + 4] != tag:
        raise ValueError('Expected %s record in playlist dataset' % tag.decode())
    head, size = u32(body, offset + 4), u32(body, offset + 8)
    if head < 12 or size < head or offset + size > len(body):
        raise ValueError('Invalid %s record length' % tag.decode())
    return head, size


class PodcastPlaylist:
    # The Podcasts menu groups episodes by group-header entries in the podcast playlist of the
    # type-3 playlist dataset, not by album. Each episode entry keeps its header ID at offset 32.
    def __init__(self, dataset, groups, entries):
        self.dataset = dataset
        self.groups = groups  # group ID -> header title
        self.entries = entries  # numeric track ID -> body offset of the group reference

    @classmethod
    def load(cls, root):
        datasets = [c for c in root.children if c.tag == b'mhsd' and u32(c.header, 12) == 3]
        if len(datasets) != 1:
            return None
        dataset = datasets[0]
        body = dataset.body
        # Like mhlt, the mhlp header keeps a child count at offset 8 instead of a total length.
        if len(body) < 12 or body[:4] != b'mhlp' or not 12 <= u32(body, 4) <= len(body):
            raise ValueError('Playlist dataset has no mhlp')
        offset, found = u32(body, 4), None
        for _ in range(u32(body, 8)):
            head, size = record(body, offset, b'mhyp')
            if head < 44:
                raise ValueError('Unsupported playlist header')
            if struct.unpack_from('<H', body, offset + 42)[0] == 1:
                if found is not None:
                    raise ValueError('Multiple podcast playlists; unsupported')
                found = cls.scan(dataset, offset + head, u32(body, offset + 12), u32(body, offset + 16))
            offset += size
        if offset != len(body):
            raise ValueError('Unparsed bytes in playlist dataset')
        return found

    @classmethod
    def scan(cls, dataset, cursor, mhods, mhips):
        body = dataset.body
        for _ in range(mhods):
            cursor += record(body, cursor, b'mhod')[1]
        groups, entries = {}, {}
        for _ in range(mhips):
            head, size = record(body, cursor, b'mhip')
            if head < 36:
                raise ValueError('Unsupported podcast playlist entry')
            group_id, track_id = u32(body, cursor + 20), u32(body, cursor + 24)
            inner, titles = cursor + head, []
            for _ in range(u32(body, cursor + 12)):
                mhod_size = record(body, inner, b'mhod')[1]
                if u32(body, inner + 12) == 1:
                    titles.append(text_of(Chunk(body[inner:inner + mhod_size])))
                inner += mhod_size
            if inner != cursor + size:
                raise ValueError('Podcast playlist entry length mismatch')
            if struct.unpack_from('<H', body, cursor + 16)[0]:
                if len(titles) != 1 or group_id in groups:
                    raise ValueError('Invalid podcast group header')
                groups[group_id] = titles[0]
            else:
                if track_id in entries:
                    raise ValueError('Duplicate podcast playlist entry')
                entries[track_id] = cursor + 32
            cursor += size
        return cls(dataset, groups, entries)

    def group_of(self, track):
        offset = self.entries.get(u32(track.header, 0x10))
        if offset is None:
            return ''
        return self.groups.get(u32(self.dataset.body, offset), '')

    def link(self, track, name):
        offset = self.entries.get(u32(track.header, 0x10))
        if offset is None:
            raise ValueError('Track is not in the Podcasts playlist; only synced podcast episodes can be linked')
        ids = [gid for gid, title in self.groups.items() if title == name]
        if not ids:
            ids = [gid for gid, title in self.groups.items() if title.casefold() == name.casefold()]
        if len(ids) != 1:
            raise ValueError('Podcast group %r not found or ambiguous; existing groups: %s'
                             % (name, ', '.join(sorted(self.groups.values())) or 'none'))
        old = self.group_of(track)
        body = bytearray(self.dataset.body)
        put32(body, offset, ids[0])
        self.dataset.body = bytes(body)
        return old, self.groups[ids[0]]


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


def gf_multiply(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = ((a << 1) ^ (0x11B if a & 0x80 else 0)) & 255
        b >>= 1
    return result


def sboxes():
    forward = []
    for x in range(256):
        inv = 0 if x == 0 else 1
        for _ in range(254 if x else 0):
            inv = gf_multiply(inv, x)
        value = inv ^ 0x63
        for shift in range(1, 5):
            value ^= ((inv << shift) | (inv >> (8 - shift))) & 255
        forward.append(value)
    reverse = [0] * 256
    for i, x in enumerate(forward):
        reverse[x] = i
    return forward, reverse


def hash58(data, guid):
    # HASH58 key derivation follows the format documented by libgpod.
    from math import gcd
    forward, reverse = sboxes()
    material = bytearray.fromhex('6723fe304533f890992107c1d012b2a10781')
    for a, b in zip(guid[::2], guid[1::2]):
        n = a * b // gcd(a, b) if a and b else 1
        material.extend([forward[n >> 8], reverse[n >> 8], forward[n & 255], reverse[n & 255]])
    key = hashlib.sha1(material).digest()
    normalized = bytearray(data)
    normalized[24:32] = bytes(8)
    normalized[50:70] = bytes(20)
    normalized[88:108] = bytes(20)
    return hmac.new(key, normalized, hashlib.sha1).digest()


def hash72_digest(data):
    normalized = bytearray(data)
    for start, size in [(24, 8), (88, 20), (114, 46)]:
        normalized[start:start + size] = bytes(size)
    return hashlib.sha1(normalized).digest()


def aes(data, decrypt=False, iv=None):
    cmd = ['/usr/bin/openssl', 'enc', '-aes-128-cbc' if iv is not None else '-aes-128-ecb',
           '-K', AES_KEY, '-nopad']
    if decrypt:
        cmd.append('-d')
    if iv is not None:
        cmd.extend(['-iv', iv.hex()])
    result = subprocess.run(cmd, input=data, capture_output=True, check=True)
    return result.stdout


def validate_signature(data, guid):
    if struct.unpack_from('<H', data, 48)[0] != 1:
        raise ValueError('Only HASH58 Classic databases are supported')
    if hash58(data, guid) != data[88:108]:
        raise ValueError('Original HASH58 invalid: wrong device GUID or damaged source')
    signature = data[114:160]
    if signature == bytes(46):
        return None
    if signature[:2] != b'\x01\x00':
        raise ValueError('Unsupported HASH72 signature')
    digest = hash72_digest(data)
    first = aes(signature[14:30], decrypt=True)
    iv = bytes(a ^ b for a, b in zip(first, digest[:16]))
    if aes(signature[14:], decrypt=True, iv=iv) != digest + signature[2:14]:
        raise ValueError('Source HASH72 is inconsistent')
    return iv, signature[2:14]


def sign(data, guid, signature_info):
    result = bytearray(data)
    if signature_info is not None:
        iv, random = signature_info
        result[114:160] = b'\x01\x00' + random + aes(hash72_digest(result) + random, iv=iv)
    result[88:108] = hash58(result, guid)
    validate_signature(result, guid)
    return bytes(result)


def device_guid(value, source):
    if value is None:
        candidates = [source.parent.parent / 'Device' / 'SysInfo', Path('/Volumes/iPod/iPod_Control/Device/SysInfo')]
        for path in candidates:
            if path.is_file():
                match = re.search(r'FirewireGuid\s*:\s*(?:0x)?([0-9a-fA-F]{16})', path.read_text())
                if match:
                    value = match.group(1)
                    break
    if value is None:
        raise ValueError('Supply --firewire-id with the device GUID from Device/SysInfo')
    result = bytes.fromhex(value.removeprefix('0x'))
    if len(result) != 8:
        raise ValueError('FireWire ID must contain 16 hex digits')
    return result


def integer(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.lower() in ['true', 'false']:
            return int(value.lower() == 'true')
        return int(value, 16 if value.lower().startswith('0x') else 10)
    raise ValueError('Expected an integer')


def media_type(value, current):
    if isinstance(value, bool):
        raise ValueError('media_type requires a number or named flags, not a boolean')
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError('media_type requires a number or named flags')
    tokens = [token.strip().lower() for token in value.split(',')]
    if not tokens or any(not token for token in tokens):
        raise ValueError('Empty media_type flag')
    relative = tokens[0][0] in '+-'
    result = current if relative else 0
    for token in tokens:
        signed = token[0] in '+-'
        if signed != relative:
            raise ValueError('Use only names/numbers or only signed flags in media_type')
        name = token[1:] if signed else token
        if name in MEDIA_TYPES:
            bits = MEDIA_TYPES[name]
        else:
            try:
                bits = int(name, 16 if name.startswith('0x') else 10)
            except ValueError:
                raise ValueError('Unknown media_type flag: ' + name) from None
        if not 0 <= bits <= 0xFFFFFFFF:
            raise ValueError('media_type flags must fit in an unsigned 32-bit value')
        if relative and token[0] == '-':
            result &= ~bits
        else:
            result |= bits
    return result


def attribute(track, field, playlist=None):
    if field in TEXT or field == 'location':
        return get_text(track, TEXT[field] if field in TEXT else 2)[0]
    if field == 'persistent_id':
        return persistent_id(track)
    if field == 'podcast_group':
        return playlist.group_of(track) if playlist else ''
    offset, fmt = (0x10, 'I') if field == 'track_id' else NUMBER[field][:2]
    return struct.unpack_from('<' + fmt, track.header, offset)[0]


def parse_filter(expression):
    match = re.fullmatch(r'([a-z_][a-z_0-9]*)\s*(!~|!=|>=|<=|=|~|>|<|&)(.*)', expression, re.DOTALL)
    if not match:
        raise ValueError('Use --filter FIELD=VALUE (or !=, ~, !~, >, >=, <, <=, &)')
    field, op, value = match.groups()
    if field not in set(TEXT) | set(NUMBER) | {'location', 'track_id', 'persistent_id', 'podcast_group'}:
        raise ValueError('Unknown filter attribute: ' + field)
    text = field in TEXT or field in {'location', 'persistent_id', 'podcast_group'}
    if text:
        if op not in {'=', '!=', '~', '!~'}:
            raise ValueError('Text filters support =, !=, ~ and !~')
        value = value.casefold()
    else:
        if op in {'~', '!~'} or (op == '&' and field != 'media_type'):
            raise ValueError('Numeric filters support comparisons; & is for media_type only')
        if field == 'media_type':
            if any(token.strip().startswith(('+', '-')) for token in value.split(',')):
                raise ValueError('Media type filters require absolute names or numeric masks')
            value = media_type(value, 0)
        else:
            value = integer(value)
    return field, op, value


def matches_filter(track, condition, playlist=None):
    field, op, wanted = condition
    actual = attribute(track, field, playlist)
    if isinstance(actual, str):
        actual = actual.casefold()
    if op == '=':
        return actual == wanted
    if op == '!=':
        return actual != wanted
    if op == '~':
        return wanted in actual
    if op == '!~':
        return wanted not in actual
    if op == '>':
        return actual > wanted
    if op == '>=':
        return actual >= wanted
    if op == '<':
        return actual < wanted
    if op == '<=':
        return actual <= wanted
    return (actual & wanted) == wanted


def edit_track(track, changes, experimental, playlist=None):
    report = []
    for field, value in changes.items():
        if field not in TEXT and field not in NUMBER and field != 'podcast_group':
            raise ValueError('Unknown or unsupported field: ' + field)
        if field in EXPERIMENTAL and not experimental:
            raise ValueError('%s requires --experimental: derived Apple indexes are preserved, not rebuilt' % field)
        if field == 'podcast_group':
            if not isinstance(value, str):
                raise ValueError('podcast_group must be the title of an existing group header')
            if playlist is None:
                raise ValueError('No podcast playlist found in the type-3 playlist dataset')
            old, value = playlist.link(track, value)
        elif field in TEXT:
            if not isinstance(value, str):
                raise ValueError('Text values must be strings')
            old = set_text(track, TEXT[field], value)
        else:
            offset, fmt, minimum, maximum = NUMBER[field]
            old = struct.unpack_from('<' + fmt, track.header, offset)[0]
            value = media_type(value, old) if field == 'media_type' else integer(value)
            if not minimum <= value <= maximum:
                raise ValueError('%s must be between %s and %s' % (field, minimum, maximum))
            struct.pack_into('<' + fmt, track.header, offset, value)
        if old != value:
            report.append({'field': field, 'before': old, 'after': value})
    return report


def write_exclusive(path, data):
    path = path.resolve()
    if path == Path('/Volumes') or Path('/Volumes') in path.parents:
        raise ValueError('Output must be a local candidate, not a mounted device')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'view', 'edit', 'fields'])
    parser.add_argument('--input', type=Path, default=Path('/Volumes/iPod/iPod_Control/iTunes/iTunesDB'))
    parser.add_argument('--output', type=Path, help='Write a NEW local file; omit for preview')
    parser.add_argument('--firewire-id')
    parser.add_argument('--id', action='append', default=[], help='Persistent hex ID shown by list; repeatable')
    parser.add_argument('--match', help='Case-insensitive title substring')
    parser.add_argument('--filter', action='append', default=[], metavar='FIELD=VALUE',
                        help='List/view: attribute comparison; repeat to require all filters')
    parser.add_argument('--podcasts', action='store_true', help='Select all existing audio podcasts')
    parser.add_argument('--all-matches', action='store_true', help='Allow --match to select more than one track')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    parser.add_argument('--edits', type=Path, help='JSON array of {id, set} objects; cannot combine with selectors')
    parser.add_argument('--experimental', action='store_true', help='Allow edits that may leave derived indexes stale')
    args = parser.parse_args()
    if args.filter and args.action not in {'list', 'view'}:
        raise ValueError('--filter is supported in list and view modes only')
    if args.action == 'view':
        if args.set or args.edits or args.output:
            raise ValueError('view is read-only; --set, --edits and --output are not supported')
        if not (args.id or args.match is not None or args.filter or args.podcasts):
            raise ValueError('Select one track with --id, --match or --filter')
    filters = [parse_filter(value) for value in args.filter]
    if args.action == 'fields':
        print('Numeric: ' + ', '.join(NUMBER))
        print('Media types: ' + ', '.join(MEDIA_TYPES))
        print('media_type accepts a number, comma-separated names, or signed flags: -music,+podcast')
        print('Text: ' + ', '.join(TEXT))
        print('Additional list/view attributes: track_id, persistent_id, location, podcast_group')
        print('podcast_group links a synced episode to an existing Podcasts-menu group header by title')
        print('Experimental: ' + ', '.join(sorted(EXPERIMENTAL)))
        return
    data = args.input.read_bytes()
    root, tracks = parse(data)
    playlist = PodcastPlaylist.load(root)
    guid = device_guid(args.firewire_id, args.input)
    signature_info = validate_signature(data, guid)
    by_id = {persistent_id(t): t for t in tracks}
    requested = {'%016X' % int(x, 16) for x in args.id}
    missing = sorted(requested - by_id.keys())
    if missing and args.action != 'edit':
        raise ValueError('Persistent ID not found: ' + ', '.join(missing))
    # Edit mode skips missing IDs with a warning so a batch continues.
    for pid in missing:
        print('Warning: persistent ID not found, skipped: ' + pid, file=sys.stderr)
    selected = [t for t in tracks if (not requested or persistent_id(t) in requested)
                and (args.match is None or args.match.casefold() in get_text(t, 1)[0].casefold())
                and (not args.podcasts or u32(t.header, 0xD0) in (4, 5))
                and all(matches_filter(t, condition, playlist) for condition in filters)]
    if args.action == 'view':
        if not selected:
            raise ValueError('No track matched the selection')
        if len(selected) != 1:
            raise ValueError('Selection matched %d tracks; use a persistent ID or narrower filters' % len(selected))
        track = selected[0]
        details = {field: attribute(track, field, playlist)
                   for field in ['persistent_id', 'track_id', 'location', 'podcast_group', *TEXT, *NUMBER]}
        mask = details['media_type']
        details['media_type_hex'] = '0x%08X' % mask
        details['media_type_flags'] = [name for name, bits in MEDIA_TYPES.items() if mask & bits == bits]
        details['media_type_unknown_bits'] = '0x%08X' % (mask & ~sum(MEDIA_TYPES.values()))
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return
    if args.action == 'list':
        print('PERSISTENT ID     TYPE  TITLE')
        for t in selected:
            print('%s  0x%02X  %s' % (persistent_id(t), u32(t.header, 0xD0), get_text(t, 1)[0]))
        return
    if args.edits:
        if args.id or args.match is not None or args.podcasts or args.set:
            raise ValueError('--edits cannot be combined with selectors or --set')
        jobs = json.loads(args.edits.read_text())
        if not isinstance(jobs, list):
            raise ValueError('--edits must contain an array')
    else:
        if not (requested or args.match is not None or args.podcasts) or not args.set:
            raise ValueError('Select tracks with --id, --match or --podcasts and supply --set')
        if args.match is not None and len(selected) > 1 and not args.all_matches:
            raise ValueError('Title match is ambiguous; use an ID or --all-matches')
        values = {}
        for item in args.set:
            key, sep, value = item.partition('=')
            if not sep or key in values:
                raise ValueError('Use distinct FIELD=VALUE assignments')
            values[key] = value
        jobs = [{'id': persistent_id(t), 'set': values} for t in selected]
    if not jobs:
        raise ValueError('No tracks selected')
    reports, seen = [], set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {'id', 'set'} or not isinstance(job['id'], str):
            raise ValueError('Each edit needs a hex string id and a set object')
        pid = '%016X' % int(job['id'], 16)
        if pid in seen or not isinstance(job['set'], dict) or not job['set']:
            raise ValueError('Duplicate ID or empty/invalid set object')
        seen.add(pid)
        if pid not in by_id:
            print('Warning: persistent ID not found, skipped: ' + pid, file=sys.stderr)
            missing.append(pid)
            continue
        track = by_id[pid]
        title = get_text(track, 1)[0]
        changes = edit_track(track, job['set'], args.experimental, playlist)
        if changes:
            reports.append({'id': pid, 'title': title, 'changes': changes})
    result = root.render()
    if result != data:
        result = sign(result, guid, signature_info)
    parse(result)
    report = {'input': str(args.input.resolve()), 'source_sha256': hashlib.sha256(data).hexdigest(),
              'result_sha256': hashlib.sha256(result).hexdigest(), 'source_bytes': len(data),
              'result_bytes': len(result), 'changes': reports, 'skipped_ids': missing,
              'finder_tested': False,
              'note': 'Apple indexes, album tables, preferences and playlists are preserved, except podcast_group links edited here. Text/grouping edits may leave caches stale.'}
    if args.output:
        if args.output.resolve() == args.input.resolve():
            raise ValueError('Input and output must differ')
        if not reports:
            raise ValueError('No changes; no output written')
        if args.input.read_bytes() != data:
            raise ValueError('Source changed during edit; retry from a stable copy')
        write_exclusive(args.output, result)
        report['output'] = str(args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, struct.error, subprocess.CalledProcessError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        sys.exit(1)
