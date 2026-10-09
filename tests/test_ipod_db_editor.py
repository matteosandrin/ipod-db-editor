import contextlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from ipod_db_editor.cli import run
from ipod_db_editor.device import device_guid
from ipod_db_editor.fields import FIELDS, media_type
from ipod_db_editor.files import write_exclusive
from ipod_db_editor.itunesdb import get_text, parse, persistent_id, put32, u32
from ipod_db_editor.podcasts import PodcastPlaylist
from ipod_db_editor.signing import sign, validate_signature
from ipod_db_editor.tracks import attribute, edit_track, matches_filter, parse_filter

ROOT = Path(__file__).resolve().parent.parent
GUID = bytes.fromhex('000A27002503D1F0')
BASE = ROOT / 'evidence/before-edits.iTunesDB'
UNLINKED = ROOT / 'evidence/unlinked-groups.iTunesDB'


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.data = BASE.read_bytes()
        self.root, self.tracks = parse(self.data)
        self.signature = validate_signature(self.data, GUID)

    def test_exact_noop_and_signatures_on_all_evidence(self):
        for name in ['before-edits', 'latest-backup', 'current', 'unlinked-groups']:
            data = (ROOT / 'evidence' / (name + '.iTunesDB')).read_bytes()
            root, _ = parse(data)
            self.assertEqual(root.render(), data)
            info = validate_signature(data, GUID)
            self.assertEqual(sign(data, GUID, info), data)

    def test_podcast_patch_changes_only_four_bytes_and_signatures(self):
        expected = bytearray(self.data)
        positions = []
        offset = self.root.hlen
        for dataset in self.root.children:
            if dataset.tag == b'mhsd' and u32(dataset.header, 12) == 1:
                cursor = offset + dataset.hlen + dataset.children[0].hlen
                for track in dataset.children[0].children:
                    if u32(track.header, 0xD0) == 5:
                        positions.append(cursor + 0xD0)
                        expected[cursor + 0xD0] = 4
                        edit_track(track, {'media_type': '-music'})
                    cursor += len(track.raw)
            offset += len(dataset.raw)
        self.assertEqual(len(positions), 4)
        self.assertEqual(self.root.render(), bytes(expected))
        result = sign(self.root.render(), GUID, self.signature)
        differences = {i for i, (a, b) in enumerate(zip(self.data, result)) if a != b}
        self.assertTrue(set(positions) <= differences)
        self.assertTrue(differences <= set(positions) | set(range(88, 108)) | set(range(114, 160)))
        self.assertEqual(len(result), len(self.data))
        self.assertEqual(validate_signature(result, GUID), self.signature)

    def test_unicode_growth_and_shrink_preserve_other_tracks_and_datasets(self):
        target = self.tracks[-1]
        original_children = [c.render() for c in target.children]
        original_header = bytes(target.header)
        original_tracks = [c.render() for c in self.tracks[:-1]]
        opaque = [c.render() for c in self.root.children if c.children is None]
        for value in ['A much longer title ' * 30 + '\U0001f3b5 caf\u00e9', 'X', '']:
            edit_track(target, {'title': value})
            result = sign(self.root.render(), GUID, self.signature)
            root, tracks = parse(result)
            self.assertEqual(get_text(tracks[-1], 1)[0], value)
            self.assertEqual([c.render() for c in tracks[:-1]], original_tracks)
            self.assertEqual([c.render() for c in root.children if c.children is None], opaque)
            self.assertEqual(bytes(target.header), original_header)
            self.assertEqual([c.render() for c in target.children if u32(c.header, 12) != 1],
                             [c for c in original_children if u32(c, 12) != 1])
            self.assertEqual(validate_signature(result, GUID), self.signature)

    def test_add_missing_text_and_keep_opaque_records(self):
        target = next(t for t in self.tracks if get_text(t, 8)[1] is None)
        pid = persistent_id(target)
        before = [c.render() for c in target.children]
        edit_track(target, {'comment': 'new note'})
        _, tracks = parse(sign(self.root.render(), GUID, self.signature))
        changed = next(t for t in tracks if persistent_id(t) == pid)
        self.assertEqual(get_text(changed, 8)[0], 'new note')
        self.assertEqual([c.render() for c in changed.children[:-1]], before)
        self.assertEqual(u32(changed.header, 12), len(before) + 1)

    def test_wrong_guid_and_corrupt_signature_rejected(self):
        with self.assertRaises(ValueError):
            validate_signature(self.data, bytes(8))
        broken = bytearray(self.data)
        broken[-1] ^= 1
        with self.assertRaises(ValueError):
            validate_signature(broken, GUID)

    def test_guid_found_without_flag(self):
        # Each source also gives a wrong GUID, so the HASH58 check must select the right one.
        wrong = '0000000000000001'
        with tempfile.TemporaryDirectory() as folder:
            prefs = Path(folder) / 'com.apple.iPod.plist'
            device = Path(folder) / 'iPod_Control' / 'Device'
            target = Path(folder) / 'iPod_Control' / 'iTunes' / 'iTunesDB'
            device.mkdir(parents=True)
            target.parent.mkdir()
            target.write_bytes(self.data)
            (device / 'SysInfo').write_text('FirewireGuid: 0x%s\n' % wrong)
            usb = []
            with patch('ipod_db_editor.device.IPOD_PREFS', prefs), patch('ipod_db_editor.device.DEFAULT_INPUT', target), \
                    patch('ipod_db_editor.device.usb_serial_numbers', lambda: usb):
                with self.assertRaisesRegex(ValueError, '--firewire-id'):
                    device_guid(None, self.data, target)
                usb[:] = [wrong, GUID.hex().upper()]
                self.assertEqual(device_guid(None, self.data, target), GUID)
                usb.clear()
                (device / 'SysInfoExtended').write_bytes(plistlib.dumps({'FireWireGUID': GUID.hex()}))
                self.assertEqual(device_guid(None, self.data, target), GUID)
                (device / 'SysInfoExtended').unlink()
                prefs.write_bytes(plistlib.dumps({'Devices': {wrong: {}, GUID.hex().upper(): {}}}))
                self.assertEqual(device_guid(None, self.data, target), GUID)
                report = json.loads(self.run_cli(['set', '--input', str(target),
                                                  '--id', persistent_id(self.tracks[0]), 'rating=80']))
                self.assertEqual(len(report['changes']), 1)
                # An explicit flag is used as given, even when a correct GUID can be found.
                self.assertEqual(device_guid('0x' + wrong, self.data, target), bytes.fromhex(wrong))
                with self.assertRaises(ValueError):
                    self.run_cli(['set', '--input', str(target), '--firewire-id', wrong,
                                  '--id', persistent_id(self.tracks[0]), 'rating=80'])
                for bad in ['zz' * 8, 'ABC']:
                    with self.assertRaises(ValueError, msg=bad):
                        device_guid(bad, self.data, target)

    def test_bad_lengths_and_counts_rejected(self):
        for offset, value in [(8, len(self.data) - 1), (20, 0xFFFFFFFF), (4, len(self.data) + 1), (16, 1)]:
            broken = bytearray(self.data)
            put32(broken, offset, value)
            with self.assertRaises(ValueError):
                parse(broken)
        with self.assertRaises(ValueError):
            parse(self.data[:-1])

    def test_input_validation(self):
        for fields in [{'rating': 101}, {'track_id': 2}, {'title': 'bad\0text'},
                       {'year': 1.5}, {'podcast_only': True}]:
            with self.assertRaises(ValueError):
                edit_track(self.tracks[0], fields)

    def test_media_type_names_numbers_and_relative_bits(self):
        for value in ['podcast', '4', '0x04', 4]:
            self.assertEqual(media_type(value, 5), 4)
        self.assertEqual(media_type('music,podcast', 0), 5)
        self.assertEqual(media_type('video,podcast', 0), 6)
        self.assertEqual(media_type('-music', 0x100005), 0x100004)
        self.assertEqual(media_type('+podcast,-music', 1), 4)
        self.assertEqual(media_type('+0x100000', 4), 0x100004)
        for value in ['', 'unknown', 'podcast,', 'music,-podcast', True, 1.5, '0x100000000']:
            with self.assertRaises(ValueError):
                media_type(value, 5)
        track = self.tracks[0]
        result = edit_track(track, {'media_type': 'video,podcast'})
        self.assertEqual(u32(track.header, 0xD0), 6)
        self.assertEqual(result[0]['field'], 'media_type')

    def test_numeric_patch_is_exact(self):
        target = self.tracks[0]
        before = bytearray(target.render())
        edit_track(target, {'rating': 80, 'remember_position': True})
        before[0x1F] = 80
        before[0xA6] = 1
        self.assertEqual(target.render(), bytes(before))

    def test_output_is_exclusive_and_device_paths_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'candidate'
            write_exclusive(path, b'one')
            with self.assertRaises(FileExistsError):
                write_exclusive(path, b'two')
            self.assertEqual(path.read_bytes(), b'one')
        with self.assertRaises(ValueError):
            write_exclusive(Path('/Volumes/iPod/test'), b'one')

    def run_cli(self, args):
        # Keep stderr in self.errors so warnings can be checked and argparse usage stays quiet.
        stream, errors = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
                run(args)
        finally:
            self.errors = errors.getvalue()
        return stream.getvalue()

    def test_preview_does_not_change_input(self):
        output = self.run_cli(['set', '--input', str(BASE), '--firewire-id', GUID.hex(),
                               '--filter', 'media_type&podcast', 'media_type=podcast'])
        report = json.loads(output)
        self.assertEqual(len(report['changes']), 4)
        self.assertNotIn('output', report)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_set_requires_selection_assignments_and_one_output(self):
        cli = ['set', '--input', str(BASE), '--firewire-id', GUID.hex()]
        for args, error in [(['rating=80'], ValueError),
                            (['--match', 'x', 'track_id=4'], ValueError),
                            (['--match', 'x', 'rating'], ValueError),
                            (['--match', 'x', 'rating=1', 'rating=2'], ValueError),
                            (['--match', 'x'], SystemExit),
                            (['--match', 'x', 'rating=1', '-o', 'a', '--in-place'], SystemExit)]:
            with self.assertRaises(error, msg=str(args)):
                self.run_cli(cli + args)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_title_match_edits_every_matching_track(self):
        # --match has no ambiguity guard; it works like any other filter.
        wanted = {persistent_id(t) for t in self.tracks
                  if 'the' in get_text(t, 1)[0].casefold() and t.header[0x1F] != 80}
        self.assertGreater(len(wanted), 1)
        report = json.loads(self.run_cli(['set', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                          '--match', 'THE', 'rating=80']))
        self.assertEqual({c['id'] for c in report['changes']}, wanted)

    def test_report_warns_only_for_indexed_fields(self):
        cli = ['set', '--input', str(BASE), '--firewire-id', GUID.hex(), '--id', persistent_id(self.tracks[0])]
        report = json.loads(self.run_cli(cli + ['rating=80']))
        self.assertEqual(report['warnings'], [])
        report = json.loads(self.run_cli(cli + ['title=New', 'rating=80']))
        self.assertEqual(len(report['warnings']), 1)
        self.assertIn('title', report['warnings'][0])

    def test_list_filters_combine_text_numbers_and_flags(self):
        track = next(t for t in self.tracks if u32(t.header, 0xD0) == 5)
        pid = persistent_id(track)
        output = self.run_cli(['list', '--input', str(BASE),
                               '--filter', 'title~' + get_text(track, 1)[0].swapcase(),
                               '--filter', 'media_type&podcast', '--filter', 'year>=0',
                               '--filter', 'persistent_id=' + pid.lower()])
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn(pid, output)
        self.assertIn('0x05', output)
        exact = self.run_cli(['list', '--input', str(BASE), '--filter', 'media_type=podcast'])
        self.assertEqual(len(exact.splitlines()), 1)
        includes = self.run_cli(['list', '--input', str(BASE), '--filter', 'media_type&podcast'])
        self.assertEqual(len(includes.splitlines()), 5)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_list_columns_and_json(self):
        track = self.tracks[0]
        pid = persistent_id(track)
        cli = ['list', '--input', str(BASE), '--id', pid, '--columns', 'artist,year,persistent_id']
        lines = self.run_cli(cli).splitlines()
        self.assertEqual(lines[0].split(), ['PERSISTENT', 'ID', 'ARTIST', 'YEAR'])
        self.assertTrue(lines[1].startswith(pid))
        self.assertTrue(lines[1].endswith(str(attribute(track, 'year'))))
        rows = json.loads(self.run_cli(cli + ['--json']))
        self.assertEqual(rows, [{'persistent_id': pid, 'artist': get_text(track, 4)[0],
                                 'year': attribute(track, 'year')}])
        with self.assertRaises(SystemExit):
            self.run_cli(['list', '--input', str(BASE), '--columns', 'nope'])

    def test_filter_operator_semantics(self):
        track = self.tracks[0]
        edit_track(track, {'title': 'Café=Live', 'rating': 80, 'remember_position': True})
        for expression in ['title=CAFÉ=LIVE', 'title~fé=', 'title!=Other', 'title!~Other',
                           'rating>79', 'rating>=80', 'rating<81', 'rating<=80', 'rating!=0',
                           'rating=0x50', 'remember_position=true', 'track_id=' + str(u32(track.header, 16)),
                           'location=' + get_text(track, 2)[0]]:
            self.assertTrue(matches_filter(track, parse_filter(expression)), expression)
        for expression in ['title=Live', 'title!~live', 'rating>80', 'rating<80', 'rating!=80',
                           'remember_position=false']:
            self.assertFalse(matches_filter(track, parse_filter(expression)), expression)
        missing = next(t for t in self.tracks if get_text(t, 8)[1] is None)
        self.assertTrue(matches_filter(missing, parse_filter('comment=')))

    def test_invalid_filters_rejected(self):
        for expression in ['title', 'unknown=x', 'year~20', 'title>abc', 'rating=nope',
                           'rating&1', 'media_type=-music', 'media_type=unknown']:
            with self.assertRaises(ValueError, msg=expression):
                parse_filter(expression)
        with self.assertRaises(ValueError):
            self.run_cli(['list', '--input', '/nonexistent', '--filter', 'unknown=x'])

    def test_set_selects_tracks_by_filter(self):
        cli = ['set', '--input', str(BASE), '--firewire-id', GUID.hex()]
        wanted = {persistent_id(t) for t in self.tracks if u32(t.header, 0xD0) == 5}
        self.assertGreater(len(wanted), 1)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'candidate'
            report = json.loads(self.run_cli([*cli, '--filter', 'media_type=5', 'media_type=4', '-o', str(out)]))
            self.assertEqual({c['id'] for c in report['changes']}, wanted)
            _, tracks = parse(out.read_bytes())
            for track in tracks:
                if persistent_id(track) in wanted:
                    self.assertEqual(u32(track.header, 0xD0), 4)
        with self.assertRaises(ValueError):
            self.run_cli([*cli, '--filter', 'title=no such title', 'rating=80'])
        with self.assertRaises(SystemExit):
            self.run_cli(['apply', '--input', str(BASE), '--filter', 'media_type=5', 'edits.json'])
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_show_track_fields_and_combined_media_flags(self):
        track = next(t for t in self.tracks if u32(t.header, 0xD0) == 5)
        pid = persistent_id(track)
        shown = json.loads(self.run_cli(['show', '--input', str(BASE), '--id', pid]))
        self.assertEqual(len(shown), 1)
        details = shown[0]
        self.assertEqual(details['persistent_id'], pid)
        self.assertEqual(details['title'], get_text(track, 1)[0])
        self.assertEqual(details['track_id'], u32(track.header, 16))
        self.assertEqual(details['location'], get_text(track, 2)[0])
        self.assertEqual(details['media_type'], 5)
        self.assertEqual(details['media_type_flags'], ['music', 'podcast'])
        self.assertEqual(details['media_type_unknown_bits'], '0x00000000')
        self.assertEqual(set(details), set(FIELDS) | {'media_type_hex', 'media_type_flags',
                                                           'media_type_unknown_bits'})
        self.assertEqual(BASE.read_bytes(), self.data)
        filtered = json.loads(self.run_cli(['show', '--input', str(BASE), '--match', details['title'],
                                            '--filter', 'media_type&podcast', '--filter', 'persistent_id=' + pid]))
        self.assertEqual(filtered, [details])

    def test_show_prints_every_match_and_rejects_write_options(self):
        cli = ['show', '--input', str(BASE)]
        self.assertEqual(len(json.loads(self.run_cli(cli))), len(self.tracks))
        self.assertEqual(len(json.loads(self.run_cli(cli + ['--match', '']))), len(self.tracks))
        self.assertEqual(json.loads(self.run_cli(cli + ['--match', 'NO SUCH TRACK 999999'])), [])
        self.assertEqual(json.loads(self.run_cli(cli + ['--id', 'FFFFFFFFFFFFFFFF'])), [])
        self.assertIn('FFFFFFFFFFFFFFFF', self.errors)
        for extra in [['rating=80'], ['-o', '/tmp/unused-candidate'], ['--in-place'],
                      ['--firewire-id', GUID.hex()], ['--id', 'not-hex']]:
            with self.assertRaises(SystemExit, msg=str(extra)):
                self.run_cli(cli + ['--match', ''] + extra)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_apply_json_batch_and_real_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'edits.json'
            out = Path(folder) / 'candidate'
            path.write_text(json.dumps([{'id': persistent_id(self.tracks[0]), 'set': {'rating': 80}}]))
            report = json.loads(self.run_cli(['apply', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                              str(path), '-o', str(out)]))
            self.assertEqual(len(report['changes']), 1)
            _, tracks = parse(out.read_bytes())
            self.assertEqual(tracks[0].header[0x1F], 80)
            for jobs in [[], {}, [{'id': 'zz', 'set': {'rating': 1}}], [{'id': 'AB', 'set': {}}],
                         [{'id': 'AB', 'set': {'rating': 1}}, {'id': '0xab', 'set': {'rating': 2}}]]:
                path.write_text(json.dumps(jobs))
                with self.assertRaises(ValueError, msg=str(jobs)):
                    self.run_cli(['apply', '--input', str(BASE), '--firewire-id', GUID.hex(), str(path)])
            self.assertEqual(BASE.read_bytes(), self.data)

    def test_in_place_backs_up_and_replaces_input(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'iTunesDB'
            target.write_bytes(self.data)
            args = ['set', '--input', str(target), '--firewire-id', GUID.hex(),
                    '--id', persistent_id(self.tracks[0]), 'rating=80']
            with self.assertRaises(ValueError):
                self.run_cli(args + ['-o', str(target)])
            backups = Path(folder) / 'backups'
            with patch('ipod_db_editor.files.BACKUPS', backups):
                report = json.loads(self.run_cli(args + ['--in-place']))
            backup = Path(report['backup'])
            self.assertEqual(backup.parent, backups.resolve())
            self.assertTrue(backup.name.startswith('iTunesDB.backup-'))
            self.assertEqual(backup.read_bytes(), self.data)
            _, tracks = parse(target.read_bytes())
            self.assertEqual(tracks[0].header[0x1F], 80)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['backups', 'iTunesDB'])
            self.assertEqual([p.name for p in backups.iterdir()], [backup.name])
            with self.assertRaises(ValueError):
                self.run_cli(args + ['--in-place'])

    def test_missing_ids_warn_and_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'edits.json'
            path.write_text(json.dumps([{'id': 'FFFFFFFFFFFFFFFF', 'set': {'rating': 80}},
                                        {'id': persistent_id(self.tracks[0]), 'set': {'rating': 80}}]))
            report = json.loads(self.run_cli(['apply', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                              str(path)]))
        self.assertEqual(len(report['changes']), 1)
        self.assertEqual(report['skipped_ids'], ['FFFFFFFFFFFFFFFF'])
        self.assertIn('Warning: persistent ID not found, skipped: FFFFFFFFFFFFFFFF', self.errors)
        report = json.loads(self.run_cli(['set', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                          '--id', 'FFFFFFFFFFFFFFFF', '--id', persistent_id(self.tracks[0]),
                                          'rating=80']))
        self.assertEqual(len(report['changes']), 1)
        self.assertEqual(report['skipped_ids'], ['FFFFFFFFFFFFFFFF'])
        output = self.run_cli(['list', '--input', str(BASE), '--id', 'FFFFFFFFFFFFFFFF'])
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn('FFFFFFFFFFFFFFFF', self.errors)
        with self.assertRaises(ValueError):
            self.run_cli(['set', '--input', str(BASE), '--firewire-id', GUID.hex(),
                          '--id', 'FFFFFFFFFFFFFFFF', 'rating=80'])
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_podcast_group_show_filter_and_link(self):
        data = UNLINKED.read_bytes()
        root, tracks = parse(data)
        playlist = PodcastPlaylist.load(root)
        self.assertEqual(sorted(playlist.groups.values()), ['Acquired', 'Decoder Ring', 'Middlebrow'])
        by_title = {get_text(t, 1)[0]: t for t in tracks}
        orphan = by_title['MrBeast Literary Society']
        pid = persistent_id(orphan)
        self.assertEqual(playlist.group_of(by_title['Vanguard']), 'Acquired')
        self.assertEqual(playlist.group_of(orphan), '')
        cli = ['--input', str(UNLINKED)]
        unlinked = self.run_cli(['list', *cli, '--filter', 'media_type&podcast', '--filter', 'podcast_group='])
        self.assertEqual(len(unlinked.splitlines()), 3)
        self.assertIn(pid, unlinked)
        self.assertEqual(len(self.run_cli(['list', *cli, '--filter', 'podcast_group=acquired']).splitlines()), 3)
        self.assertEqual(json.loads(self.run_cli(['show', *cli, '--id', pid]))[0]['podcast_group'], '')
        with self.assertRaises(ValueError):
            edit_track(orphan, {'podcast_group': 'Middlebrow'}, None)
        for value in ['Nope', 1]:
            with self.assertRaises(ValueError):
                edit_track(orphan, {'podcast_group': value}, playlist)
        music = next(t for t in tracks if u32(t.header, 0xD0) == 1)
        with self.assertRaises(ValueError):
            edit_track(music, {'podcast_group': 'Middlebrow'}, playlist)
        # The playlist dataset bytes are unchanged after the rejected edits.
        self.assertEqual(root.render(), data)
        report = edit_track(orphan, {'podcast_group': 'middlebrow'}, playlist)
        self.assertEqual(report, [{'field': 'podcast_group', 'before': '', 'after': 'Middlebrow'}])
        self.assertEqual(edit_track(orphan, {'podcast_group': 'Middlebrow'}, playlist), [])
        result = sign(root.render(), GUID, validate_signature(data, GUID))
        self.assertEqual(len(result), len(data))
        index = root.children.index(playlist.dataset)
        start = root.hlen + sum(len(c.raw) for c in root.children[:index]) + playlist.dataset.hlen
        reference = start + playlist.entries[u32(orphan.header, 0x10)]
        differences = {i for i, (a, b) in enumerate(zip(data, result)) if a != b}
        patched = differences - set(range(88, 108)) - set(range(114, 160))
        self.assertTrue(patched)
        self.assertTrue(patched <= set(range(reference, reference + 4)))
        root, tracks = parse(result)
        relinked = next(t for t in tracks if persistent_id(t) == pid)
        self.assertEqual(PodcastPlaylist.load(root).group_of(relinked), 'Middlebrow')
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'candidate'
            report = json.loads(self.run_cli(['set', *cli, '--firewire-id', GUID.hex(), '--id', pid,
                                              'podcast_group=Middlebrow', '-o', str(out)]))
            self.assertEqual(report['changes'][0]['changes'][0]['after'], 'Middlebrow')
            self.assertEqual(report['warnings'], [])
            self.assertEqual(out.read_bytes(), result)
            details = json.loads(self.run_cli(['show', '--input', str(out), '--id', pid]))[0]
            self.assertEqual(details['podcast_group'], 'Middlebrow')
        with self.assertRaises(ValueError):
            self.run_cli(['set', *cli, '--firewire-id', GUID.hex(), '--id', pid, 'podcast_group=Nope'])
        self.assertEqual(UNLINKED.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
