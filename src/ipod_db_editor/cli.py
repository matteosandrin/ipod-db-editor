"""Command-line interface: fields, list, show, set and apply."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

from ipod_db_editor import __doc__ as DESCRIPTION
from ipod_db_editor.device import DEFAULT_INPUT, device_guid
from ipod_db_editor import ui
from ipod_db_editor.fields import EDITABLE, FIELDS, INDEXED
from ipod_db_editor.files import replace_in_place, write_exclusive
from ipod_db_editor.itunesdb import get_text, parse, persistent_id
from ipod_db_editor.podcasts import PodcastPlaylist
from ipod_db_editor.signing import sign, validate_signature
from ipod_db_editor.tracks import attribute, details, edit_track, matches_filter, parse_filter


def hex_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0x)?[0-9a-fA-F]{1,16}', value):
        raise ValueError('Invalid persistent ID: %r' % (value,))
    return '%016X' % int(value, 16)


def columns(value):
    names = [name.strip() for name in value.split(',') if name.strip()]
    unknown = [name for name in names if name not in FIELDS]
    if unknown:
        raise argparse.ArgumentTypeError('unknown field(s): %s (see the fields command)' % ', '.join(unknown))
    return [name for name in names if name != 'persistent_id']


def parse_assignments(items):
    values = {}
    for item in items:
        key, sep, value = item.partition('=')
        if not sep or key in values:
            raise ValueError('Use distinct FIELD=VALUE assignments: ' + item)
        if key not in EDITABLE:
            raise ValueError('Unknown or read-only field: ' + key)
        values[key] = value
    return values


def load_jobs(path):
    jobs = json.loads(path.read_text())
    if not isinstance(jobs, list) or not jobs:
        raise ValueError('The edits file must contain a non-empty array')
    result, seen = [], set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {'id', 'set'}:
            raise ValueError('Each edit needs an id and a set object')
        pid = hex_id(job['id'])
        if pid in seen or not isinstance(job['set'], dict) or not job['set']:
            raise ValueError('Duplicate ID or empty/invalid set object: ' + pid)
        seen.add(pid)
        result.append((pid, job['set']))
    return result


def warn_missing(pid):
    ui.warn('persistent ID not found, skipped: ' + pid)


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_list(tracks, names, as_json, playlist, total):
    names = ['persistent_id', *names]
    rows = [{name: attribute(t, name, playlist) for name in names} for t in tracks]
    if as_json:
        print_json(rows)
    else:
        ui.print_tracks(rows, names, total)


def write_changes(args, data, root, by_id, playlist, jobs, skipped):
    if args.output and args.output.resolve() == args.input.resolve():
        raise ValueError('Input and output must differ; use --in-place to replace the input')
    with ui.status('Finding the device GUID...'):
        guid = device_guid(args.firewire_id, data, args.input)
        signature_info = validate_signature(data, guid)
    reports, edited = [], set()
    for pid, changes in jobs:
        if pid not in by_id:
            warn_missing(pid)
            skipped.append(pid)
            continue
        track = by_id[pid]
        title = get_text(track, 1)[0]
        report = edit_track(track, changes, playlist)
        if report:
            reports.append({'id': pid, 'title': title, 'changes': report})
            edited.update(change['field'] for change in report)
    result = root.render()
    if result != data:
        result = sign(result, guid, signature_info)
    parse(result)
    stale = sorted(edited & INDEXED)
    report = {'input': str(args.input.resolve()), 'source_sha256': hashlib.sha256(data).hexdigest(),
              'result_sha256': hashlib.sha256(result).hexdigest(), 'source_bytes': len(data),
              'result_bytes': len(result), 'changes': reports, 'skipped_ids': skipped,
              'finder_tested': False,
              'warnings': ['Apple indexes derived from %s were kept, not rebuilt; sort order, grouping or '
                           'smart playlists can be stale' % ', '.join(stale)] if stale else []}
    if args.output or args.in_place:
        if not reports:
            raise ValueError('No changes; nothing written')
        if args.input.read_bytes() != data:
            raise ValueError('Source changed during edit; retry from a stable copy')
    if args.output:
        write_exclusive(args.output, result)
        report['output'] = str(args.output.resolve())
    if args.in_place:
        report['backup'] = str(replace_in_place(args.input.resolve(), data, result).resolve())
        report['output'] = str(args.input.resolve())
    if args.json:
        print_json(report)
    else:
        ui.print_report(report)


EPILOG = '''\
examples:
  %(prog)s list --filter 'artist~Justice' --filter 'year>=2007'
  %(prog)s list --filter 'media_type&podcast' --columns album,podcast_group
  %(prog)s show --id A242151529FFD1D0
  %(prog)s set --id A242151529FFD1D0 'title=Wanta Fanda' rating=80
  %(prog)s set --filter media_type=5 media_type=podcast --in-place
  %(prog)s apply edits.json -o candidates/iTunesDB

Selection options combine with AND; repeated --id values combine with OR.
set and apply only preview changes unless you give -o or --in-place.'''


def add_write_options(parser):
    # A parent parser would drop this group title, so add the group to each command.
    group = parser.add_argument_group('output (default: preview only)')
    group.add_argument('--firewire-id', metavar='HEX',
                       help='Device GUID for signing; found automatically when omitted')
    exclusive = group.add_mutually_exclusive_group()
    exclusive.add_argument('-o', '--output', type=Path, metavar='PATH', help='Write the result to a NEW local file')
    exclusive.add_argument('--in-place', action='store_true',
                           help='Back up --input with a datestamp, then replace it')


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--input', type=Path, default=DEFAULT_INPUT, metavar='PATH',
                        help='iTunesDB to read (default: the mounted iPod)')
    select = argparse.ArgumentParser(add_help=False)
    group = select.add_argument_group('selection (all options must match)')
    group.add_argument('--id', action='append', default=[], type=hex_id, metavar='HEX',
                       help='Persistent ID shown by list; repeat to select several')
    group.add_argument('--filter', action='append', default=[], metavar='EXPR',
                       help="Field comparison, for example 'artist~Justice' or 'year>=2007'; repeatable")
    group.add_argument('--match', action='append', default=[], metavar='TEXT',
                       help='Same as --filter title~TEXT')

    parser = argparse.ArgumentParser(prog='ipod-db-editor', description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')
    commands.add_parser('fields', help='Show the fields that list, show, --filter and set accept')
    sub = commands.add_parser('list', parents=[common, select], help='Print a table of matching tracks')
    sub.add_argument('--columns', type=columns, default='media_type,title', metavar='FIELDS',
                     help='Comma-separated fields to show after the ID (default: media_type,title)')
    sub.add_argument('--json', action='store_true', help='Print a JSON array instead of a table')
    sub = commands.add_parser('show', parents=[common, select], help='Print every field of matching tracks')
    sub.add_argument('--json', action='store_true', help='Print a JSON array instead of panels')
    sub = commands.add_parser('set', parents=[common, select], help='Change fields on matching tracks')
    sub.add_argument('assignments', nargs='+', metavar='FIELD=VALUE', help='Field to change; see the fields command')
    sub.add_argument('--json', action='store_true', help='Print the change report as JSON')
    add_write_options(sub)
    sub = commands.add_parser('apply', parents=[common], help='Apply per-track changes from a JSON file')
    sub.add_argument('edits', type=Path, metavar='EDITS.json', help='Array of {"id": HEX, "set": {FIELD: VALUE}}')
    sub.add_argument('--json', action='store_true', help='Print the change report as JSON')
    add_write_options(sub)
    return parser


def run(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == 'fields':
        ui.print_fields()
        return
    filters = [parse_filter(expression) for expression in getattr(args, 'filter', [])]
    filters += [parse_filter('title~' + text) for text in getattr(args, 'match', [])]
    assignments = parse_assignments(args.assignments) if args.command == 'set' else None
    with ui.status('Reading %s...' % args.input):
        data = args.input.read_bytes()
        root, tracks = parse(data)
        playlist = PodcastPlaylist.load(root)
    by_id = {persistent_id(t): t for t in tracks}
    if args.command == 'apply':
        write_changes(args, data, root, by_id, playlist, load_jobs(args.edits), [])
        return
    ids = set(args.id)
    missing = sorted(ids - by_id.keys())
    for pid in missing:
        warn_missing(pid)
    selected = [t for t in tracks if (not ids or persistent_id(t) in ids)
                and all(matches_filter(t, condition, playlist) for condition in filters)]
    if args.command == 'list':
        print_list(selected, args.columns, args.json, playlist, len(tracks))
    elif args.command == 'show':
        shown = [details(t, playlist) for t in selected]
        if args.json:
            print_json(shown)
        else:
            ui.print_details(shown)
    else:
        if not (ids or filters):
            raise ValueError('Select tracks with --id, --filter or --match')
        if not selected:
            raise ValueError('No track matched the selection')
        jobs = [(persistent_id(t), assignments) for t in selected]
        write_changes(args, data, root, by_id, playlist, jobs, missing)


def main():
    # Console entry point. run() raises; this prints a short error and exits with status 1.
    try:
        run()
    except (ValueError, OSError, KeyError, struct.error, subprocess.CalledProcessError) as exc:
        ui.error(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        ui.err_console.print('[yellow]Cancelled.[/yellow]')
        sys.exit(130)
