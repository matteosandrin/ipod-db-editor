"""Safe writes: new local files only, or an in-place swap after a verified backup."""
from datetime import datetime
import os
from pathlib import Path

# Backups stay on the Mac, never on the iPod.
BACKUPS = Path.home() / 'Library/Application Support/ipod-db-editor/backups'


def write_exclusive(path, data):
    path = path.resolve()
    if path == Path('/Volumes') or Path('/Volumes') in path.parents:
        raise ValueError('Output must be a local candidate, not a mounted device')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new(path, data)


def write_new(path, data):
    with path.open('xb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def replace_in_place(path, original, result):
    # Keep a dated copy of the original on the Mac, then swap in the result atomically.
    backup = BACKUPS / (path.name + '.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    write_exclusive(backup, original)
    if backup.read_bytes() != original:
        raise ValueError('Backup verification failed; the database was not changed')
    temp = path.with_name(path.name + '.upload-tmp')
    write_new(temp, result)
    os.replace(temp, path)
    return backup
