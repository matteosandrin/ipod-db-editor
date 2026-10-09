"""Find the connected iPod's database and the FireWire GUID that signs it."""
from pathlib import Path
import plistlib
import re
import subprocess

from ipod_db_editor.signing import hash58

DEFAULT_INPUT = Path('/Volumes/iPod/iPod_Control/iTunes/iTunesDB')
# macOS records each connected iPod here, keyed by its FireWire GUID.
IPOD_PREFS = Path.home() / 'Library/Preferences/com.apple.iPod.plist'


def read_plist(path):
    try:
        return plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return {}


def usb_serial_numbers():
    # A connected iPod uses its FireWire GUID as the USB serial number.
    try:
        output = subprocess.run(['/usr/sbin/ioreg', '-p', 'IOUSB', '-l', '-w0'],
                                capture_output=True, text=True).stdout
    except OSError:
        return []
    return re.findall(r'"USB Serial Number" = "([0-9a-fA-F]{16})"', output)


def guid_candidates(source):
    # Yield possible GUIDs, local files first. The caller checks each against HASH58.
    for device in [source.parent.parent / 'Device', DEFAULT_INPUT.parent.parent / 'Device']:
        if (device / 'SysInfo').is_file():
            yield from re.findall(r'FirewireGuid\s*:\s*(?:0x)?([0-9a-fA-F]{16})',
                                  (device / 'SysInfo').read_text(errors='replace'))
        if (device / 'SysInfoExtended').is_file():
            yield str(read_plist(device / 'SysInfoExtended').get('FireWireGUID', ''))
    if IPOD_PREFS.is_file():
        yield from read_plist(IPOD_PREFS).get('Devices', {})
    yield from usb_serial_numbers()


def parse_guid(value):
    try:
        result = bytes.fromhex(value.removeprefix('0x'))
    except ValueError:
        result = b''
    if len(result) != 8:
        raise ValueError('FireWire ID must contain 16 hex digits')
    return result


def device_guid(value, data, source):
    if value is not None:
        return parse_guid(value)
    tried = set()
    for candidate in guid_candidates(source):
        try:
            guid = parse_guid(candidate)
        except ValueError:
            continue
        if guid not in tried and hash58(data, guid) == data[88:108]:
            return guid
        tried.add(guid)
    raise ValueError('No known device GUID matches this database (tried %d). Connect the iPod, '
                     'or supply --firewire-id with its USB serial number' % len(tried))
