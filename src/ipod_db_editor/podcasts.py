"""Podcasts-menu group links in the type-3 playlist dataset."""
import struct

from ipod_db_editor.itunesdb import Chunk, put32, text_of, u32


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
