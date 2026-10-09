"""HASH58 and HASH72 database signatures."""
import hashlib
import hmac
from math import gcd
import struct
import subprocess

AES_KEY = '618ca10dc7f57fd3b4723e08157463d7'


def gf_multiply(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = ((a << 1) ^ (0x11B if a & 0x80 else 0)) & 255
        b >>= 1
    return result


def sboxes():
    forward = []
    for x in range(256):
        inv = 0 if x == 0 else 1
        for _ in range(254 if x else 0):
            inv = gf_multiply(inv, x)
        value = inv ^ 0x63
        for shift in range(1, 5):
            value ^= ((inv << shift) | (inv >> (8 - shift))) & 255
        forward.append(value)
    reverse = [0] * 256
    for i, x in enumerate(forward):
        reverse[x] = i
    return forward, reverse


def hash58(data, guid):
    # HASH58 key derivation follows the format documented by libgpod.
    forward, reverse = sboxes()
    material = bytearray.fromhex('6723fe304533f890992107c1d012b2a10781')
    for a, b in zip(guid[::2], guid[1::2]):
        n = a * b // gcd(a, b) if a and b else 1
        material.extend([forward[n >> 8], reverse[n >> 8], forward[n & 255], reverse[n & 255]])
    key = hashlib.sha1(material).digest()
    normalized = bytearray(data)
    normalized[24:32] = bytes(8)
    normalized[50:70] = bytes(20)
    normalized[88:108] = bytes(20)
    return hmac.new(key, normalized, hashlib.sha1).digest()


def hash72_digest(data):
    normalized = bytearray(data)
    for start, size in [(24, 8), (88, 20), (114, 46)]:
        normalized[start:start + size] = bytes(size)
    return hashlib.sha1(normalized).digest()


def aes(data, decrypt=False, iv=None):
    cmd = ['/usr/bin/openssl', 'enc', '-aes-128-cbc' if iv is not None else '-aes-128-ecb',
           '-K', AES_KEY, '-nopad']
    if decrypt:
        cmd.append('-d')
    if iv is not None:
        cmd.extend(['-iv', iv.hex()])
    result = subprocess.run(cmd, input=data, capture_output=True, check=True)
    return result.stdout


def validate_signature(data, guid):
    if struct.unpack_from('<H', data, 48)[0] != 1:
        raise ValueError('Only HASH58 Classic databases are supported')
    if hash58(data, guid) != data[88:108]:
        raise ValueError('Original HASH58 invalid: wrong device GUID or damaged source')
    signature = data[114:160]
    if signature == bytes(46):
        return None
    if signature[:2] != b'\x01\x00':
        raise ValueError('Unsupported HASH72 signature')
    digest = hash72_digest(data)
    first = aes(signature[14:30], decrypt=True)
    iv = bytes(a ^ b for a, b in zip(first, digest[:16]))
    if aes(signature[14:], decrypt=True, iv=iv) != digest + signature[2:14]:
        raise ValueError('Source HASH72 is inconsistent')
    return iv, signature[2:14]


def sign(data, guid, signature_info):
    result = bytearray(data)
    if signature_info is not None:
        iv, random = signature_info
        result[114:160] = b'\x01\x00' + random + aes(hash72_digest(result) + random, iv=iv)
    result[88:108] = hash58(result, guid)
    validate_signature(result, guid)
    return bytes(result)
