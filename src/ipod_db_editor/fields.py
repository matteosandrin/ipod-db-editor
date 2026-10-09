"""Track field names, their layouts and value parsing."""

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
# Apple derives sort, group and smart-playlist indexes from these fields. Edits keep
# those indexes unchanged, so the iPod can show stale order or grouping.
INDEXED = set(TEXT) | {'track_number', 'disc_number', 'media_type'}
# All fields that list, show and --filter accept. Only EDITABLE fields accept set.
READ_ONLY = ['persistent_id', 'track_id', 'location']
EDITABLE = ['podcast_group', *TEXT, *NUMBER]
FIELDS = READ_ONLY + EDITABLE


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


def is_text(field):
    return field in TEXT or field in ('persistent_id', 'location', 'podcast_group')


def describe(field):
    if is_text(field):
        return 'text'
    if field == 'media_type':
        return 'flags'
    if field == 'track_id':
        return 'number'
    minimum, maximum = NUMBER[field][2:]
    return 'bool' if maximum == 1 else 'number %d-%d' % (minimum, maximum)
