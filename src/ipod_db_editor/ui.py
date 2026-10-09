"""Terminal output: tables, panels and one style for each field type."""
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ipod_db_editor.fields import (EDITABLE, FIELDS, INDEXED, MEDIA_TYPES, NUMBER, READ_ONLY, TEXT,
                                   describe, is_text)

console = Console(highlight=False)
# Warnings, errors and spinners go to stderr, so stdout stays clean for pipes.
err_console = Console(stderr=True, highlight=False)

MEDIA_STYLES = {'music': 'green', 'video': 'blue', 'podcast': 'magenta', 'audiobook': 'yellow',
                'music_video': 'cyan', 'tv_show': 'bright_blue'}
KNOWN_MEDIA_BITS = sum(MEDIA_TYPES.values())
ACRONYMS = {'id': 'ID', 'bpm': 'BPM', 'tv': 'TV'}


def is_bool(field):
    return field in NUMBER and NUMBER[field][3] == 1


def header(field):
    return ' '.join(ACRONYMS.get(word, word.capitalize()) for word in field.split('_'))


def justify(field):
    if is_bool(field):
        return 'center'
    if field == 'track_id' or (field in NUMBER and field not in ('media_type', 'rating')):
        return 'right'
    return 'left'


def render(field, value, style=''):
    # list, show and the change table all use this, so a value looks the same everywhere.
    if field == 'persistent_id':
        return Text(value, 'cyan')
    if field == 'media_type':
        return media_names(value, style)
    if field == 'rating':
        return stars(value, style)
    if is_bool(field):
        return Text('✓', style + ' green') if value else Text('|', style + ' dim')
    if value == '':
        return Text('—', 'dim')
    return Text(str(value), style)


def media_names(mask, style=''):
    text = Text(style=style)
    text.append('0x%02X ' % mask, 'dim')
    parts = [(name, MEDIA_STYLES[name]) for name, bits in MEDIA_TYPES.items() if mask & bits == bits]
    if mask & ~KNOWN_MEDIA_BITS:
        parts.append(('0x%X' % (mask & ~KNOWN_MEDIA_BITS), 'red'))
    for i, (name, color) in enumerate(parts or [('none', 'dim')]):
        if i:
            text.append(' | ', 'dim')
        text.append(name, color)
    return text


def stars(rating, style=''):
    text = Text(style=style)
    text.append('★' * (rating // 20), 'yellow')
    text.append('☆' * (5 - rating // 20), 'grey37')
    if rating % 20:
        text.append(' %d' % rating, 'dim')
    return text


def one_line(text):
    # Rich shrinks only wrappable columns to fit, so the column wraps and the cell truncates.
    text.no_wrap, text.overflow = True, 'ellipsis'
    return text


def plural(count, word):
    return '%d %s%s' % (count, word, '' if count == 1 else 's')


def new_table(title, caption=None):
    return Table(title=title, caption=caption, title_style='bold', header_style='bold',
                 caption_style='dim', border_style='grey42')


def status(message):
    return err_console.status(Text(message, 'dim'), spinner='dots')


def warn(message):
    err_console.print(Text.assemble(('Warning:', 'bold yellow'), ' ', message), soft_wrap=True)


def error(message):
    err_console.print(Panel(Text(message), title='[bold red]Error', title_align='left', border_style='red',
                             expand=False))


def print_empty(message):
    console.print(Panel(Text(message, 'dim'), border_style='grey42', expand=False))


def print_fields():
    table = new_table('Fields')
    table.add_column('Field', no_wrap=True)
    table.add_column('Type', style='dim', no_wrap=True)
    table.add_column('Access', no_wrap=True)
    for i, group in enumerate([READ_ONLY, ['podcast_group', *TEXT], list(NUMBER)]):
        if i:
            table.add_section()
        for field in group:
            access = Text('edit', 'green') if field in EDITABLE else Text('read', 'dim')
            if field in INDEXED:
                access.append(' *', 'yellow')
            table.add_row(field, describe(field), access)

    syntax = new_table('Values', '~ means "contains". & means "has these bits".')
    syntax.add_column('Type', no_wrap=True)
    syntax.add_column('--filter', style='cyan', no_wrap=True)
    syntax.add_column('set accepts')
    syntax.add_row('text', '= != ~ !~', 'any text')
    syntax.add_row('number', '= != > >= < <=', 'decimal or 0x hex')
    syntax.add_row('bool', '= != > >= < <=', 'true/false or 1/0')
    syntax.add_row('flags', '= != > >= < <= &', 'names, numbers or signed changes (+podcast,-music)')

    names = Text()
    for i, (name, bits) in enumerate(MEDIA_TYPES.items()):
        names.append(', ' if i else '')
        names.append(name, MEDIA_STYLES[name])
        names.append(' (%d)' % bits, 'dim')
    notes = Group(
        Text.assemble(('*', 'yellow'), ' Apple indexes derived from this field are kept, not rebuilt. '
                      'An edit can leave sort order, grouping or smart playlists stale.'),
        Text.assemble(('media_type names: ', 'bold'), names),
        Text.assemble(('--filter media_type=podcast', 'cyan'), ' is exact; ',
                      ('media_type&podcast', 'cyan'), ' means "has the podcast bit".'),
        Text.assemble(('podcast_group', 'cyan'),
                      ' links a synced episode to an existing Podcasts-menu group by title.'))
    console.print(table)
    console.print()
    console.print(syntax)
    console.print()
    console.print(Panel(notes, title='Notes', title_align='left', border_style='grey42', expand=False))


def print_tracks(rows, names, total):
    if not rows:
        print_empty('No tracks matched (%s on the iPod).' % plural(total, 'track'))
        return
    table = new_table('Tracks', '%d of %s' % (len(rows), plural(total, 'track')))
    for name in names:
        shrink = is_text(name) and name != 'persistent_id'
        table.add_column(header(name), justify=justify(name), no_wrap=not shrink)
    for row in rows:
        table.add_row(*[one_line(render(name, row[name])) for name in names])
    console.print(table)


def print_details(tracks):
    if not tracks:
        print_empty('No tracks matched.')
        return
    for info in tracks:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style='dim', justify='right', no_wrap=True)
        grid.add_column(overflow='fold')
        for field in FIELDS:
            grid.add_row(field, render(field, info[field]))
        title = Text(info['title'] or 'Untitled', 'bold')
        console.print(Panel(grid, title=title, title_align='left', border_style='grey42', expand=False))


def print_report(report):
    written = 'output' in report
    changes = sum(len(track['changes']) for track in report['changes'])
    if report['changes']:
        console.print(changes_table(report['changes'], written))
    console.print(summary_panel(report, changes, written))
    for message in report['warnings']:
        err_console.print(Panel(Text(message), title='[bold yellow]Warning', title_align='left',
                                border_style='yellow', expand=False))


def changes_table(tracks, written):
    table = new_table('Changes' if written else 'Changes (preview)')
    table.add_column('Track', no_wrap=True, max_width=24)
    table.add_column('Field', no_wrap=True)
    table.add_column('Before', overflow='fold')
    table.add_column('')
    table.add_column('After', overflow='fold')
    for i, track in enumerate(tracks):
        if i:
            table.add_section()
        # Title on row 1, ID on row 2. The title can truncate, the ID never does.
        labels = [one_line(Text(track['title'] or 'Untitled', 'bold')), Text(track['id'], 'cyan')]
        for j in range(max(len(track['changes']), 2)):
            cells = ['', '', '', '']
            if j < len(track['changes']):
                change = track['changes'][j]
                field = change['field']
                cells = [field, render(field, change['before'], 'red strike'), Text('→', 'dim'),
                         render(field, change['after'], 'bold green')]
            table.add_row(labels[j] if j < 2 else '', *cells)
    return table


def summary_panel(report, changes, written):
    if not changes:
        return Panel(Text('No changes: every value already matches.'), title='Preview',
                     title_align='left', border_style='yellow', expand=False)
    counts = '%s on %s.' % (plural(changes, 'change'), plural(len(report['changes']), 'track'))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style='dim', no_wrap=True)
    grid.add_column(overflow='fold')
    if written:
        grid.add_row('Output', Text(report['output']))
    if 'backup' in report:
        grid.add_row('Backup', Text(report['backup']))
    grid.add_row('SHA-256', Text.assemble(report['source_sha256'][:12], (' → ', 'dim'),
                                          report['result_sha256'][:12]))
    grid.add_row('Size', Text.assemble('{:,}'.format(report['source_bytes']), (' → ', 'dim'),
                                       '{:,} bytes '.format(report['result_bytes']),
                                       ('(%+d)' % (report['result_bytes'] - report['source_bytes']), 'dim')))
    if written:
        return Panel(Group(Text(counts), Text(), grid), title='[green]✓[/green] Written',
                     title_align='left', border_style='green', expand=False)
    hint = Text.assemble('Add ', ('-o PATH', 'cyan'), ' or ', ('--in-place', 'cyan'), ' to write.')
    return Panel(Group(Text(counts + ' Nothing written.'), Text(), grid, Text(), hint), title='Preview',
                 title_align='left', border_style='yellow', expand=False)
