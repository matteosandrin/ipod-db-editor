import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import ipod_db_editor as edit

ROOT = Path(__file__).parent
GUID = bytes.fromhex('000A27002503D1F0')
BASE = ROOT / 'evidence/before-edits.iTunesDB'


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.data = BASE.read_bytes()
        self.root, self.tracks = edit.parse(self.data)
        self.signature = edit.validate_signature(self.data, GUID)

    def test_exact_noop_and_signatures_on_all_evidence(self):
        for name in ['before-edits', 'latest-backup', 'current']:
            data = (ROOT / 'evidence' / (name + '.iTunesDB')).read_bytes()
            root, _ = edit.parse(data)
            self.assertEqual(root.render(), data)
            info = edit.validate_signature(data, GUID)
            self.assertEqual(edit.sign(data, GUID, info), data)

    def test_podcast_patch_changes_only_four_bytes_and_signatures(self):
        expected = bytearray(self.data)
        positions = []
        offset = self.root.hlen
        for dataset in self.root.children:
            if dataset.tag == b'mhsd' and edit.u32(dataset.header, 12) == 1:
                cursor = offset + dataset.hlen + dataset.children[0].hlen
                for track in dataset.children[0].children:
                    if edit.u32(track.header, 0xD0) == 5:
                        positions.append(cursor + 0xD0)
                        expected[cursor + 0xD0] = 4
                        edit.edit_track(track, {'media_type': '-music'}, True)
                    cursor += len(track.raw)
            offset += len(dataset.raw)
        self.assertEqual(len(positions), 4)
        self.assertEqual(self.root.render(), bytes(expected))
        result = edit.sign(self.root.render(), GUID, self.signature)
        differences = {i for i, (a, b) in enumerate(zip(self.data, result)) if a != b}
        self.assertTrue(set(positions) <= differences)
        self.assertTrue(differences <= set(positions) | set(range(88, 108)) | set(range(114, 160)))
        self.assertEqual(len(result), len(self.data))
        self.assertEqual(edit.validate_signature(result, GUID), self.signature)

    def test_unicode_growth_and_shrink_preserve_other_tracks_and_datasets(self):
        target = self.tracks[-1]
        original_children = [c.render() for c in target.children]
        original_header = bytes(target.header)
        original_tracks = [c.render() for c in self.tracks[:-1]]
        opaque = [c.render() for c in self.root.children if c.children is None]
        for value in ['A much longer title ' * 30 + '\U0001f3b5 caf\u00e9', 'X', '']:
            edit.edit_track(target, {'title': value}, True)
            result = edit.sign(self.root.render(), GUID, self.signature)
            root, tracks = edit.parse(result)
            self.assertEqual(edit.get_text(tracks[-1], 1)[0], value)
            self.assertEqual([c.render() for c in tracks[:-1]], original_tracks)
            self.assertEqual([c.render() for c in root.children if c.children is None], opaque)
            self.assertEqual(bytes(target.header), original_header)
            self.assertEqual([c.render() for c in target.children if edit.u32(c.header, 12) != 1],
                             [c for c in original_children if edit.u32(c, 12) != 1])
            self.assertEqual(edit.validate_signature(result, GUID), self.signature)

    def test_add_missing_text_and_keep_opaque_records(self):
        target = next(t for t in self.tracks if edit.get_text(t, 8)[1] is None)
        pid = edit.persistent_id(target)
        before = [c.render() for c in target.children]
        edit.edit_track(target, {'comment': 'new note'}, True)
        _, tracks = edit.parse(edit.sign(self.root.render(), GUID, self.signature))
        changed = next(t for t in tracks if edit.persistent_id(t) == pid)
        self.assertEqual(edit.get_text(changed, 8)[0], 'new note')
        self.assertEqual([c.render() for c in changed.children[:-1]], before)
        self.assertEqual(edit.u32(changed.header, 12), len(before) + 1)

    def test_wrong_guid_and_corrupt_signature_rejected(self):
        with self.assertRaises(ValueError):
            edit.validate_signature(self.data, bytes(8))
        broken = bytearray(self.data)
        broken[-1] ^= 1
        with self.assertRaises(ValueError):
            edit.validate_signature(broken, GUID)

    def test_bad_lengths_and_counts_rejected(self):
        for offset, value in [(8, len(self.data) - 1), (20, 0xFFFFFFFF), (4, len(self.data) + 1), (16, 1)]:
            broken = bytearray(self.data)
            edit.put32(broken, offset, value)
            with self.assertRaises(ValueError):
                edit.parse(broken)
        with self.assertRaises(ValueError):
            edit.parse(self.data[:-1])

    def test_input_validation(self):
        for fields, experimental in [({'title': 'X'}, False), ({'rating': 101}, False),
                                      ({'track_id': 2}, True), ({'title': 'bad\0text'}, True),
                                      ({'year': 1.5}, False), ({'podcast_only': True}, False)]:
            with self.assertRaises(ValueError):
                edit.edit_track(self.tracks[0], fields, experimental)

    def test_media_type_names_numbers_and_relative_bits(self):
        for value in ['podcast', '4', '0x04', 4]:
            self.assertEqual(edit.media_type(value, 5), 4)
        self.assertEqual(edit.media_type('music,podcast', 0), 5)
        self.assertEqual(edit.media_type('video,podcast', 0), 6)
        self.assertEqual(edit.media_type('-music', 0x100005), 0x100004)
        self.assertEqual(edit.media_type('+podcast,-music', 1), 4)
        self.assertEqual(edit.media_type('+0x100000', 4), 0x100004)
        for value in ['', 'unknown', 'podcast,', 'music,-podcast', True, 1.5, '0x100000000']:
            with self.assertRaises(ValueError):
                edit.media_type(value, 5)
        track = self.tracks[0]
        with self.assertRaises(ValueError):
            edit.edit_track(track, {'media_type': 'podcast'}, False)
        result = edit.edit_track(track, {'media_type': 'video,podcast'}, True)
        self.assertEqual(edit.u32(track.header, 0xD0), 6)
        self.assertEqual(result[0]['field'], 'media_type')

    def test_numeric_patch_is_exact(self):
        target = self.tracks[0]
        before = bytearray(target.render())
        edit.edit_track(target, {'rating': 80, 'remember_position': True}, False)
        before[0x1F] = 80
        before[0xA6] = 1
        self.assertEqual(target.render(), bytes(before))

    def test_output_is_exclusive_and_device_paths_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'candidate'
            edit.write_exclusive(path, b'one')
            with self.assertRaises(FileExistsError):
                edit.write_exclusive(path, b'two')
            self.assertEqual(path.read_bytes(), b'one')
        with self.assertRaises(ValueError):
            edit.write_exclusive(Path('/Volumes/iPod/test'), b'one')

    def run_cli(self, args):
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['ipod_db_editor.py'] + args), contextlib.redirect_stdout(stream):
            edit.main()
        return stream.getvalue()

    def test_preview_does_not_change_input(self):
        output = self.run_cli(['edit', '--input', str(BASE), '--firewire-id', GUID.hex(),
                               '--podcasts', '--set', 'media_type=podcast', '--experimental'])
        report = json.loads(output)
        self.assertEqual(len(report['changes']), 4)
        self.assertNotIn('output', report)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_ambiguous_title_match_rejected(self):
        with self.assertRaises(ValueError):
            self.run_cli(['edit', '--input', str(BASE), '--firewire-id', GUID.hex(),
                          '--match', '', '--set', 'rating=80'])

    def test_list_filters_combine_text_numbers_and_flags(self):
        track = next(t for t in self.tracks if edit.u32(t.header, 0xD0) == 5)
        pid = edit.persistent_id(track)
        output = self.run_cli(['list', '--input', str(BASE), '--firewire-id', GUID.hex(),
                               '--filter', 'title~' + edit.get_text(track, 1)[0].swapcase(),
                               '--filter', 'media_type&podcast', '--filter', 'year>=0',
                               '--filter', 'persistent_id=' + pid.lower()])
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn(pid, output)
        exact = self.run_cli(['list', '--input', str(BASE), '--firewire-id', GUID.hex(),
                              '--filter', 'media_type=podcast'])
        self.assertEqual(len(exact.splitlines()), 1)
        includes = self.run_cli(['list', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                 '--filter', 'media_type&podcast'])
        self.assertEqual(len(includes.splitlines()), 5)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_filter_operator_semantics(self):
        track = self.tracks[0]
        edit.edit_track(track, {'title': 'Caf\u00e9=Live', 'rating': 80, 'remember_position': True}, True)
        for expression in ['title=CAF\u00c9=LIVE', 'title~f\u00e9=', 'title!=Other', 'title!~Other',
                           'rating>79', 'rating>=80', 'rating<81', 'rating<=80', 'rating!=0',
                           'rating=0x50', 'remember_position=true', 'track_id=' + str(edit.u32(track.header, 16)),
                           'location=' + edit.get_text(track, 2)[0]]:
            self.assertTrue(edit.matches_filter(track, edit.parse_filter(expression)), expression)
        for expression in ['title=Live', 'title!~live', 'rating>80', 'rating<80', 'rating!=80',
                           'remember_position=false']:
            self.assertFalse(edit.matches_filter(track, edit.parse_filter(expression)), expression)
        missing = next(t for t in self.tracks if edit.get_text(t, 8)[1] is None)
        self.assertTrue(edit.matches_filter(missing, edit.parse_filter('comment=')))

    def test_invalid_filters_and_edit_mode_rejected(self):
        for expression in ['title', 'unknown=x', 'year~20', 'title>abc', 'rating=nope',
                           'rating&1', 'media_type=-music', 'media_type=unknown']:
            with self.assertRaises(ValueError, msg=expression):
                edit.parse_filter(expression)
        with self.assertRaises(ValueError):
            self.run_cli(['edit', '--filter', 'title~anything'])
        with self.assertRaises(ValueError):
            self.run_cli(['list', '--input', '/nonexistent', '--filter', 'unknown=x'])

    def test_view_track_fields_and_combined_media_flags(self):
        track = next(t for t in self.tracks if edit.u32(t.header, 0xD0) == 5)
        pid = edit.persistent_id(track)
        details = json.loads(self.run_cli(['view', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                          '--id', pid]))
        self.assertEqual(details['persistent_id'], pid)
        self.assertEqual(details['title'], edit.get_text(track, 1)[0])
        self.assertEqual(details['track_id'], edit.u32(track.header, 16))
        self.assertEqual(details['location'], edit.get_text(track, 2)[0])
        self.assertEqual(details['media_type'], 5)
        self.assertEqual(details['media_type_flags'], ['music', 'podcast'])
        self.assertEqual(details['media_type_unknown_bits'], '0x00000000')
        self.assertTrue(set(edit.TEXT) | set(edit.NUMBER) <= details.keys())
        self.assertEqual(BASE.read_bytes(), self.data)
        filtered = json.loads(self.run_cli(['view', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                           '--match', details['title'], '--filter', 'media_type&podcast',
                                           '--filter', 'persistent_id=' + pid]))
        self.assertEqual(filtered, details)

    def test_view_rejects_ambiguous_missing_and_write_options(self):
        for selection in [[], ['--match', ''], ['--match', 'NO SUCH TRACK 999999'],
                          ['--id', 'FFFFFFFFFFFFFFFF'], ['--match', '', '--all-matches'],
                          ['--match', '', '--set', 'rating=80'],
                          ['--match', '', '--output', '/tmp/unused-candidate'],
                          ['--match', '', '--edits', '/tmp/unused-edits']]:
            with self.assertRaises(ValueError, msg=str(selection)):
                self.run_cli(['view', '--input', str(BASE), '--firewire-id', GUID.hex()] + selection)
        self.assertEqual(BASE.read_bytes(), self.data)

    def test_json_batch_and_real_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'edits.json'
            out = Path(folder) / 'candidate'
            path.write_text(json.dumps([{'id': edit.persistent_id(self.tracks[0]), 'set': {'rating': 80}}]))
            report = json.loads(self.run_cli(['edit', '--input', str(BASE), '--firewire-id', GUID.hex(),
                                             '--edits', str(path), '--output', str(out)]))
            self.assertEqual(len(report['changes']), 1)
            _, tracks = edit.parse(out.read_bytes())
            self.assertEqual(tracks[0].header[0x1F], 80)
            self.assertEqual(BASE.read_bytes(), self.data)


if __name__ == '__main__':
    unittest.main()
