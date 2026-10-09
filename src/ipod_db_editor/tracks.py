"""Read, filter and edit the fields of one track."""
import re
import struct

from ipod_db_editor.fields import (EDITABLE, FIELDS, MEDIA_TYPES, NUMBER, TEXT, integer,
                                   is_text, media_type)
from ipod_db_editor.itunesdb import get_text, persistent_id, set_text


def attribute(track, field, playlist=None):
    if field in TEXT or field == 'location':
        return get_text(track, TEXT[field] if field in TEXT else 2)[0]
    if field == 'persistent_id':
        return persistent_id(track)
    if field == 'podcast_group':
        return playlist.group_of(track) if playlist else ''
    offset, fmt = (0x10, 'I') if field == 'track_id' else NUMBER[field][:2]
    return struct.unpack_from('<' + fmt, track.header, offset)[0]


def details(track, playlist=None):
    result = {field: attribute(track, field, playlist) for field in FIELDS}
    mask = result['media_type']
    result['media_type_hex'] = '0x%08X' % mask
    result['media_type_flags'] = [name for name, bits in MEDIA_TYPES.items() if mask & bits == bits]
    result['media_type_unknown_bits'] = '0x%08X' % (mask & ~sum(MEDIA_TYPES.values()))
    return result


def parse_filter(expression):
    match = re.fullmatch(r'([a-z_][a-z_0-9]*)\s*(!~|!=|>=|<=|=|~|>|<|&)(.*)', expression, re.DOTALL)
    if not match:
        raise ValueError('Use --filter FIELD=VALUE (or !=, ~, !~, >, >=, <, <=, &)')
    field, op, value = match.groups()
    if field not in FIELDS:
        raise ValueError('Unknown filter field: ' + field)
    if is_text(field):
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


def edit_track(track, changes, playlist=None):
    report = []
    for field, value in changes.items():
        if field not in EDITABLE:
            raise ValueError('Unknown or read-only field: ' + field)
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
