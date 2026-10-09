# iPod Database Editor

`ipod_db_editor.py` edits selected track records in the iPod database, while preserving every other byte except enclosing lengths/counts and database signatures. The goal of this script is to make surgical edits to the iPod database, while preserving the ability to sync with Finder.

It needs Python 3.9+ and the macOS `/usr/bin/openssl` executable, with no pip packages or reference checkout dependency.

## Commands

```
ipod_db_editor.py fields                                  # every field, its type, and whether it is editable
ipod_db_editor.py list  [SELECT] [--columns F,...] [--json]  # table of matching tracks
ipod_db_editor.py show  [SELECT]                          # every field of matching tracks, as JSON
ipod_db_editor.py set   [SELECT] FIELD=VALUE... [WRITE]   # change fields on matching tracks
ipod_db_editor.py apply EDITS.json [WRITE]                # different changes per track
```

Every command except `fields` takes `--input PATH`. It defaults to the connected iPod's `iPod_Control/iTunes/iTunesDB`. Run any command with `--help` for its options. Options go after the command name.

The rules are the same for every command:

- **Selection** (`list`, `show`, `set`): `--id HEX`, `--filter EXPR` and `--match TEXT` all must match (AND). Repeated `--id` values combine with OR. `--match TEXT` is the same as `--filter 'title~TEXT'`. A missing ID gives a warning, not an error.
- **Counts never fail a read.** `list` and `show` print all matching tracks, which can be none. Only `set` fails when nothing matches. `set` also needs at least one selector, so it never edits every track by accident.
- **Writes are explicit.** `set` and `apply` show a preview unless you give `-o PATH` (write a new local file) or `--in-place` (back up, then replace `--input`).
- **Only writes need the device GUID, and it is found automatically.** `set` and `apply` try the GUIDs in `Device/SysInfo` and `Device/SysInfoExtended` (next to `--input` or on the mounted iPod), every iPod in `~/Library/Preferences/com.apple.iPod.plist`, and the USB serial number of any connected iPod. They use the first one that matches the database's HASH58 signature. If none matches, give `--firewire-id HEX`; it is then used as given. `list` and `show` do not use it.

## Find tracks

```sh
python3 ipod_db_editor.py list --input evidence/before-edits.iTunesDB --match Middlebrow
```

The ID shown is the track's persistent ID, preserved across the iOpenPod rewrites. Use it instead of a row number or the numeric track ID that iOpenPod reassigned.

By default `list` shows the media type and title. Use `--columns` to choose other fields, and `--json` for a JSON array:

```sh
python3 ipod_db_editor.py list --filter 'media_type&podcast' --columns album,podcast_group
```

## Show all fields

```sh
python3 ipod_db_editor.py show --input evidence/before-edits.iTunesDB --id A242151529FFD1D0
```

`show` prints a JSON array with one object per matching track. Each object contains every field from `fields`. Media type also shows as a hex value, decoded flag names and any unknown bits. Missing text fields show as empty strings, as they do in filters. This shows the editor's known fields, not undocumented binary fields.

## Filter by fields

Use repeatable `--filter` options for any field that `fields` lists:

```sh
python3 ipod_db_editor.py list --filter 'artist~Justice' --filter 'year>=2007'
```

- Text: `=` and `!=` compare the entire value; `~` and `!~` test a substring. All text comparisons are case-insensitive. An absent text field is treated as empty, so `--filter 'comment='` matches absent or empty comments.
- Numbers: `=`, `!=`, `>`, `>=`, `<`, `<=`. Decimal and `0x` values work; bool fields also accept `true` and `false`.
- Media types: `--filter 'media_type=podcast'` matches exactly `0x04`; `--filter 'media_type&podcast'` matches any track with the podcast bit, including music+podcast and video+podcast. Comma-separated names work too; `&` requires all specified bits. Signed edit operations are not allowed in filters.
- Quote expressions so the shell does not interpret `<`, `>` or `&`. Text after the operator is literal, including spaces and equals signs.

Unknown fields or invalid operators fail with an error before the database is read.

## Edit fields

`set` applies the same assignments to every selected track. Run `list` with the same selectors first, and omit `-o`/`--in-place` to preview the change report:

```sh
python3 ipod_db_editor.py set \
  --input evidence/before-edits.iTunesDB \
  --id A242151529FFD1D0 \
  'title=Wanta Fanda' rating=80 remember_position=true \
  -o candidates/renamed.iTunesDB
```

Put the `FIELD=VALUE` assignments together, before or after the options.

Text edits preserve the existing text encoding and unknown record fields. Longer and shorter strings are supported, and enclosing sizes are updated. Missing text records are added using the standard UTF-16LE layout. Empty values set empty strings rather than deleting records. Existing opaque records, numeric IDs, album tables and playlists remain unchanged.

`rating` uses 0-100; 80 represents four stars. Bool fields accept true/false or 1/0. Every assignment changes only the named field; duplicate/shadow fields are not automatically rewritten.

`media_type` is a bitmask, so it supports exact assignment or changes to selected bits. Media type edits change the database field, not the encoded file format or podcast subscriptions:

```sh
media_type=podcast                 # Replace the mask with 0x04.
media_type=music,podcast           # Replace the mask with 0x05.
media_type=0x04                    # Numeric assignment.
media_type=-music                  # Clear music; preserve every other bit.
media_type=+podcast,-music         # Set podcast and clear music.
```

Names: music (1), video (2), podcast (4), audiobook (8), music_video (32), tv_show (64). Numeric masks can represent other bits. Within one assignment, use either absolute values or signed additions/removals. The same strings work in JSON.

File locations, track IDs, encoded audio properties, artwork and unknown offsets are intentionally not editable.

### Stale indexes

Apple also stores sort indexes, album/artist grouping, artwork associations and smart-playlist state. This script preserves those records; it does not regenerate them. A title can keep its old sort position; album or artist changes can leave stale group associations; new lyrics do not automatically set the separate lyrics flag. Explicit sort-title overrides remain unchanged unless separately edited.

`fields` marks the fields that feed these indexes with `*` (all text fields, `track_number`, `disc_number` and `media_type`). When an edit changes one of them, the report's `warnings` list names the fields. Finder acceptance, browsing consistency and persistence after the next Apple sync are not guaranteed. This is a surgical patch tool, not a complete general-purpose sync engine.

## Podcast group links

The iPod's Podcasts menu does not group episodes by album or artist. It reads the Podcasts playlist in the database, which holds one group-header entry per podcast and, for each episode entry, a reference to its header. An episode whose reference is 0 is shown under a fallback group named "Root", even when a header with the right name exists. Finder has been observed writing such unlinked entries.

```sh
python3 ipod_db_editor.py list --filter 'media_type&podcast' --filter 'podcast_group='

python3 ipod_db_editor.py set \
  --input evidence/unlinked-groups.iTunesDB \
  --id 5F413F281B4090BF \
  podcast_group=Middlebrow \
  -o candidates/relinked.iTunesDB
```

`podcast_group` is the title of the episode's group header, or an empty string when the episode is unlinked or not in the Podcasts playlist. It appears in `show` and can be filtered like a text field. Setting it patches the four-byte reference in the episode's playlist entry so it points at the existing header with that title (case-insensitive). The header must already exist; this tool does not create group headers, and it does not add tracks to the playlist. Only the type-3 playlist dataset, which the iPod Classic reads, is patched.

## Different changes per track

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
python3 ipod_db_editor.py apply edits.json \
  --input evidence/before-edits.iTunesDB \
  -o candidates/bulk-edited.iTunesDB
```

IDs that are not in the database are skipped with a warning and listed in the report's `skipped_ids`.

## Writing the result

`set` and `apply` print a JSON change report with source/result SHA256 hashes. Redirect stdout to a separate report file if desired. Without an output option, nothing is written.

- `-o PATH` writes a new local file. It refuses to overwrite an existing file or to write under `/Volumes`.
- `--in-place` copies the current database to `backups/iTunesDB.backup-YYYYMMDD-HHMMSS` in this folder on the Mac (never on the iPod), then replaces `--input` with the edited database. The report includes the backup path.

Both fail if there are no changes, and you can use only one of them:

```sh
python3 ipod_db_editor.py set --filter 'media_type=5' media_type=podcast --in-place
```

## Installing and testing

Without `--in-place`, the script creates local candidates only. Installing one manually means replacing `iPod_Control/iTunes/iTunesDB` on the mounted device, which is a separate operation. First preserve the current database and matching preferences outside the iPod, and make sure neither Finder nor iOpenPod is syncing. Eject and reconnect after installation to test Finder acceptance.

For the existing warning, the useful controlled sequence is: confirm Finder accepts the untouched pre-edit baseline with its matching saved preferences; then test the podcast-only candidate; then try broader metadata edits one at a time. Do not substitute a factory restore for these tests. Start future edits from a Finder-accepted database. Apple syncing can overwrite device-side metadata using the Mac library, so make the corresponding source-library change where possible if it must persist.

## Validation

Run `python3 -m unittest -v test_ipod_db_editor` from this folder. The 26 tests use the saved evidence and local temporary files, without writing to the iPod. They cover exact no-op round trips and signatures for all four copies, four-byte-only podcast changes, Unicode string growth/shrink, preservation of unrelated records, adding missing text, scalar edits, podcast group links, malformed data, incorrect GUIDs, automatic GUID lookup, filters and selection, list columns, stale-index warnings, exclusive output, in-place replacement with backups and JSON batches.

HASH58 is implemented with the libgpod-documented key derivation. HASH72 retains the original signature's IV/random bytes and recomputes its digest/encryption using OpenSSL; it does not create a new device identity. The tests confirm that signing an unmodified source produces its exact original bytes. Apple's private database validation has not been reproduced.
