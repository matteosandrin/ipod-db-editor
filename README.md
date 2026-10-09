# iPod Database Editor

Edit track metadata directly in an iPod Classic's `iTunesDB`, without breaking Finder sync.

Requires macOS and Python 3.9+. No other dependencies.

## Install

```sh
pip install .
```

## Quick start

With your iPod connected:

```sh
ipod-db-editor list --match "sultans of swing"                              # find tracks by title
ipod-db-editor set media_type=music --match "sultans of swing"              # preview a change
ipod-db-editor set media_type=music --match "sultans of swing" --in-place   # apply it
```

`--in-place` backs up the current database to `~/Library/Application Support/ipod-db-editor/backups/` before writing.

## Commands

| Command | What it does |
| --- | --- |
| `fields` | List every field and whether it is editable |
| `list` | Table of matching tracks (`--columns a,b`, `--json`) |
| `show` | Every field of matching tracks, as JSON |
| `set FIELD=VALUE...` | Change fields on matching tracks |
| `apply EDITS.json` | Apply different changes per track |

All commands except `fields` read the connected iPod by default. Use `--input PATH` to work on a local copy instead. Run any command with `--help` for details.

## Selecting tracks

- `--id HEX` - persistent track ID (shown by `list`). Repeat for several tracks.
- `--match TEXT` - title contains TEXT.
- `--filter EXPR` - any field from `fields`. Repeat to combine (all must match).

```sh
ipod-db-editor list --filter 'artist~dire straits' --filter 'year>=1985'
```

Operators: `=` `!=` (exact), `~` `!~` (contains), `>` `>=` `<` `<=` (numbers). Text matching ignores case. Quote filters so the shell does not eat `<`, `>` or `&`.

## Editing

```sh
ipod-db-editor set 'title=Sultans of Swing' rating=80 --id A242151529FFD1D0
```

- `rating` is 0-100 (80 = four stars). Booleans take `true`/`false`.
- `media_type` takes names: `music`, `video`, `podcast`, `audiobook`, `music_video`, `tv_show`.
  - `media_type=podcast` replaces the value.
  - `media_type=+podcast,-music` adds/removes bits.
  - `--filter 'media_type&podcast'` matches any track with the podcast bit.
- `podcast_group='99% Invisible'` moves an episode under an existing podcast in the iPod's Podcasts menu. Useful for episodes that show up under "Root". Find them with `--filter 'media_type&podcast' --filter 'podcast_group='`.

For different changes per track, use `apply` with a JSON file:

```json
[
  {"id": "A14F1B1379489891", "set": {"title": "Ten Thousand Years", "media_type": "podcast"}},
  {"id": "A242151529FFD1D0", "set": {"title": "Vexillonaire"}}
]
```

## Saving changes

`set` and `apply` only preview by default. To write, add one of:

- `--in-place` - back up, then replace the database on the iPod.
- `-o PATH` - write to a new local file.

## Caveats

- The iPod keeps its own sort and grouping indexes, which this tool does not rebuild. After changing titles, artists, albums etc., items may sort or group under old values. The tool warns when this can happen.
- Your next Finder sync may overwrite edits with your Mac library's metadata.
- Make sure Finder is not syncing while you edit in place.

## Development

```sh
pip install -e .
python3 -m unittest discover -v -s tests
```
