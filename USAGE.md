# Surgical iPod metadata editor

`ipod_db_editor.py` edits selected track records while preserving every other byte except enclosing lengths/counts and database signatures. It does not load and rewrite the library through iOpenPod. It needs Python 3.9+ and the macOS `/usr/bin/openssl` executable, with no pip packages or reference checkout dependency.

**Finder compatibility is not yet verified.** Byte preservation avoids the broad data loss identified in iOpenPod, but cannot prove compatibility with Apple's undocumented checks. This implementation supports the uncompressed version `0x75`, HASH58 database format examined on your Classic. It refuses other versions and invalid input signatures.

## Your podcast correction

A local candidate is already prepared at `candidates/podcasts-only.iTunesDB`, using `evidence/before-edits.iTunesDB` as its baseline. Exactly four non-signature bytes changed: the media type's music bit was cleared on each of the four existing podcasts. File size is unchanged at 985,524 bytes. The candidate is not installed on the iPod.

This uses the earlier saved database because editing the current iOpenPod-written database will not put its missing Apple records back. The candidate therefore does not include later title edits or the three subsequently merged plays.

To preview the correction yourself, run from this folder:

```sh
python3 ipod_db_editor.py edit \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --podcasts --set media_type=podcast --experimental
```

Omitting `--output` only prints a preview. Add `--output candidates/new-name.iTunesDB` to save a candidate. The output must be a new local file; existing files and mounted device output paths are refused. Originals are never overwritten by this script.

## Find a track

```sh
python3 ipod_db_editor.py list \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 --match Middlebrow
```

The ID shown is the track's persistent ID, preserved across the iOpenPod rewrites. Use it instead of a row number or the numeric track ID that iOpenPod reassigned. A connected iPod is the default input if `--input` is omitted. The device GUID can be read automatically from an adjacent `Device/SysInfo` or the default mounted iPod; supply the explicit GUID for standalone copies or if it cannot be detected.

## View one track's fields

```sh
python3 ipod_db_editor.py view \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --id A242151529FFD1D0
```

`view` prints a JSON object containing every supported text and numeric metadata field, the persistent and numeric track IDs, and the file location. Media type is displayed as a number, hex value, decoded flag names and any unknown bits. Missing text fields display as empty strings, as they do in filters. This displays the editor's known fields, not undocumented binary fields.

You can select by `--match` or repeatable `--filter` options instead. For example, replace `--id ...` with `--match 'Wanta Fanda'` when reading the edited candidate. Exactly one track must match; zero or multiple matches produce an error. `--all-matches` does not bypass this requirement. `view` is read-only and rejects editing/output options.

## Filter by attributes in list mode

Use repeatable `--filter` options for any supported text or numeric field, plus `track_id`, `persistent_id` and `location`. All filters must match (AND), including existing `--id`, `--match` and `--podcasts` selectors.

```sh
python3 ipod_db_editor.py list \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --filter 'artist~Justice' --filter 'year>=2007'
```

- Text: `=` and `!=` compare the entire value; `~` and `!~` test a substring. All text comparisons are case-insensitive. An absent text field is treated as empty, so `--filter 'comment='` matches absent or empty comments.
- Numbers: `=`, `!=`, `>`, `>=`, `<`, `<=`. Decimal and `0x` values work; Boolean fields also accept `true` and `false`.
- Media types: `--filter 'media_type=podcast'` matches exactly `0x04`; `--filter 'media_type&podcast'` matches any track with the podcast bit, including music+podcast and video+podcast. Comma-separated names work too; `&` requires all specified bits. Signed edit operations are not allowed in filters.
- Quote expressions so the shell does not interpret `<`, `>` or `&`. Text after the operator is literal, including spaces and equals signs.

Filters only select rows; they do not modify the database. They are supported in `list` and `view` modes. Use `fields` to see the available attributes. Unknown fields or invalid operators fail with an error, even if there would be no matching tracks.

## Edit a title

```sh
python3 ipod_db_editor.py edit \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --id A242151529FFD1D0 \
  --set 'title=Wanta Fanda' \
  --experimental \
  --output candidates/renamed.iTunesDB
```

Text edits preserve the existing text encoding and unknown record fields. Longer and shorter strings are supported, and enclosing sizes are updated. Missing text records are added using the standard UTF-16LE layout. Empty values set empty strings rather than deleting records. Existing opaque records, numeric IDs, album tables and playlists remain unchanged.

**Why text edits are experimental:** Apple also stores sort indexes, album/artist grouping, artwork associations and smart-playlist state. This script preserves those records; it does not attempt to regenerate them. A title can retain its old sort position; album or artist changes can leave stale group associations; new lyrics do not automatically set the separate lyrics flag. Explicit sort-title overrides remain unchanged unless separately edited. Finder acceptance, browsing consistency and persistence after the next Apple sync are not guaranteed. Some numeric edits can also affect smart playlists. This is a surgical patch tool, not a complete general-purpose sync engine.

## Numeric fields and playback options

```sh
python3 ipod_db_editor.py edit \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --id A242151529FFD1D0 \
  --set rating=80 --set remember_position=true \
  --output candidates/playback-options.iTunesDB
```

`rating` uses 0-100; 80 represents four stars. Boolean fields accept true/false or 1/0. Every assignment changes only the named field; duplicate/shadow fields are not automatically rewritten. These edits still require a Finder/device acceptance test.

Supported text fields: title, album, artist, genre, comment, category, lyrics, composer, grouping, description, subtitle, show, episode, tv_network, album_artist, sort_artist, keywords, sort_title, sort_album, sort_album_artist, sort_composer, sort_show.

Supported numeric fields: rating, track_number, total_tracks, year, disc_number, total_discs, bpm, skip_when_shuffling, remember_position, media_type. Media type edits require `--experimental`; they change the database field, not the encoded file format or podcast subscriptions.

`media_type` is a bitmask, so it supports exact assignment or changes to selected bits:

```sh
--set media_type=podcast                 # Replace the mask with 0x04.
--set media_type=music,podcast           # Replace the mask with 0x05.
--set media_type=0x04                    # Numeric assignment.
--set media_type=-music                 # Clear music; preserve every other bit.
--set media_type=+podcast,-music         # Set podcast and clear music.
```

Names: music (1), video (2), podcast (4), audiobook (8), music_video (32), tv_show (64). Numeric masks can represent other bits. Within one assignment, use either absolute values or signed additions/removals. The same strings work in JSON. `--podcasts` is only a track selector; it does not edit a field. There is no separate `podcast_only` database field or editing shortcut.


List fields with `python3 ipod_db_editor.py fields`. File locations, track IDs, encoded audio properties, artwork and unknown offsets are intentionally not exposed for editing.

## Multiple tracks

Repeat `--id` to apply the same assignments to several tracks. `--match` selects by a case-insensitive title substring; multiple results require `--all-matches`. `--podcasts` explicitly selects all existing audio podcasts.

For different changes on each track, save a JSON file:

```json
[
  {
    "id": "A14F1B1379489891",
    "set": {"title": "Door to Door w/ Recho Omondi", "media_type": "podcast"}
  },
  {
    "id": "A242151529FFD1D0",
    "set": {"title": "Wanta Fanda", "media_type": "podcast"}
  }
]
```

```sh
python3 ipod_db_editor.py edit \
  --input evidence/before-edits.iTunesDB \
  --firewire-id 000A27002503D1F0 \
  --edits edits.json --experimental \
  --output candidates/bulk-edited.iTunesDB
```

An edit run prints a JSON change report with source/result SHA256 hashes. Redirect stdout to a separate report file if desired.

## Installing and testing

The script creates local candidates only. Installing one means replacing `iPod_Control/iTunes/iTunesDB` on the mounted device, which is a separate operation. First preserve the current database and matching preferences outside the iPod, and make sure neither Finder nor iOpenPod is syncing. Eject and reconnect after installation to test Finder acceptance.

For the existing warning, the useful controlled sequence is: confirm Finder accepts the untouched pre-edit baseline with its matching saved preferences; then test the podcast-only candidate; then try broader metadata edits one at a time. Do not substitute a factory restore for these tests. Start future edits from a Finder-accepted database. Apple syncing can overwrite device-side metadata using the Mac library, so make the corresponding source-library change where possible if it must persist.

## Validation

Run `python3 -m unittest -v test_ipod_db_editor` from this folder. The 18 tests use the saved evidence and local temporary files, without writing to the iPod. They cover exact no-op round trips and signatures for all three copies, four-byte-only podcast changes, Unicode string growth/shrink, preservation of unrelated records, adding missing text, scalar edits, malformed data, incorrect GUIDs, ambiguous matches, exclusive output and JSON batches.

HASH58 is implemented with the libgpod-documented key derivation. HASH72 retains the original signature's IV/random bytes and recomputes its digest/encryption using OpenSSL; it does not create a new device identity. The tests confirm that signing an unmodified source produces its exact original bytes. Apple's private database validation has not been reproduced.
