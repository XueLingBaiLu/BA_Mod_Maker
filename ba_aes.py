# -*- coding: utf-8 -*-
"""AES-256 (CBC, PKCS#7) — no external dependencies.

Uses Windows CNG (bcrypt.dll) for hardware-accelerated AES when available,
with a self-contained pure-Python T-table implementation (pyaes-style layout)
as the fallback. Verified against Node.js crypto.createCipheriv('aes-256-cbc',
...) test vectors, NIST SP 800-38A vectors, and the real game DB.
"""

__all__ = ["AES256CBC", "pkcs7_pad", "pkcs7_unpad"]

import struct
import sys

_unpack_from = struct.unpack_from
_pack_into = struct.pack_into

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_INV_SBOX = (
    0x52, 0x09, 0x6A, 0xD5, 0x30, 0x36, 0xA5, 0x38, 0xBF, 0x40, 0xA3, 0x9E, 0x81, 0xF3, 0xD7, 0xFB,
    0x7C, 0xE3, 0x39, 0x82, 0x9B, 0x2F, 0xFF, 0x87, 0x34, 0x8E, 0x43, 0x44, 0xC4, 0xDE, 0xE9, 0xCB,
    0x54, 0x7B, 0x94, 0x32, 0xA6, 0xC2, 0x23, 0x3D, 0xEE, 0x4C, 0x95, 0x0B, 0x42, 0xFA, 0xC3, 0x4E,
    0x08, 0x2E, 0xA1, 0x66, 0x28, 0xD9, 0x24, 0xB2, 0x76, 0x5B, 0xA2, 0x49, 0x6D, 0x8B, 0xD1, 0x25,
    0x72, 0xF8, 0xF6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xD4, 0xA4, 0x5C, 0xCC, 0x5D, 0x65, 0xB6, 0x92,
    0x6C, 0x70, 0x48, 0x50, 0xFD, 0xED, 0xB9, 0xDA, 0x5E, 0x15, 0x46, 0x57, 0xA7, 0x8D, 0x9D, 0x84,
    0x90, 0xD8, 0xAB, 0x00, 0x8C, 0xBC, 0xD3, 0x0A, 0xF7, 0xE4, 0x58, 0x05, 0xB8, 0xB3, 0x45, 0x06,
    0xD0, 0x2C, 0x1E, 0x8F, 0xCA, 0x3F, 0x0F, 0x02, 0xC1, 0xAF, 0xBD, 0x03, 0x01, 0x13, 0x8A, 0x6B,
    0x3A, 0x91, 0x11, 0x41, 0x4F, 0x67, 0xDC, 0xEA, 0x97, 0xF2, 0xCF, 0xCE, 0xF0, 0xB4, 0xE6, 0x73,
    0x96, 0xAC, 0x74, 0x22, 0xE7, 0xAD, 0x35, 0x85, 0xE2, 0xF9, 0x37, 0xE8, 0x1C, 0x75, 0xDF, 0x6E,
    0x47, 0xF1, 0x1A, 0x71, 0x1D, 0x29, 0xC5, 0x89, 0x6F, 0xB7, 0x62, 0x0E, 0xAA, 0x18, 0xBE, 0x1B,
    0xFC, 0x56, 0x3E, 0x4B, 0xC6, 0xD2, 0x79, 0x20, 0x9A, 0xDB, 0xC0, 0xFE, 0x78, 0xCD, 0x5A, 0xF4,
    0x1F, 0xDD, 0xA8, 0x33, 0x88, 0x07, 0xC7, 0x31, 0xB1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xEC, 0x5F,
    0x60, 0x51, 0x7F, 0xA9, 0x19, 0xB5, 0x4A, 0x0D, 0x2D, 0xE5, 0x7A, 0x9F, 0x93, 0xC9, 0x9C, 0xEF,
    0xA0, 0xE0, 0x3B, 0x4D, 0xAE, 0x2A, 0xF5, 0xB0, 0xC8, 0xEB, 0xBB, 0x3C, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2B, 0x04, 0x7E, 0xBA, 0x77, 0xD6, 0x26, 0xE1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0C, 0x7D,
)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D)


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


# T-tables (pyaes layout): state = 4 ints, each a 32-bit column (big-endian bytes)
def _build_tables():
    te0, te1, te2, te3 = [], [], [], []
    td0, td1, td2, td3 = [], [], [], []
    for i in range(256):
        s = _SBOX[i]
        t = _gmul(2, s)
        u = _gmul(3, s)
        te0.append((t << 24) | (s << 16) | (s << 8) | u)
        te1.append((u << 24) | (t << 16) | (s << 8) | s)
        te2.append((s << 24) | (u << 16) | (t << 8) | s)
        te3.append((s << 24) | (s << 16) | (u << 8) | t)
        si = _INV_SBOX[i]
        a = _gmul(0xE, si)
        b = _gmul(0x9, si)
        c = _gmul(0xD, si)
        d = _gmul(0xB, si)
        td0.append((a << 24) | (b << 16) | (c << 8) | d)
        td1.append((d << 24) | (a << 16) | (b << 8) | c)
        td2.append((c << 24) | (d << 16) | (a << 8) | b)
        td3.append((b << 24) | (c << 16) | (d << 8) | a)
    return te0, te1, te2, te3, td0, td1, td2, td3


_TE0, _TE1, _TE2, _TE3, _TD0, _TD1, _TD2, _TD3 = _build_tables()
_M32 = 0xFFFFFFFF


def _expand_key(key):
    """Returns list of round words (int per 4 bytes, big-endian)."""
    nk = len(key) // 4
    nr = nk + 6
    w = [0] * (4 * (nr + 1))
    for i in range(nk):
        w[i] = (key[4*i] << 24) | (key[4*i+1] << 16) | (key[4*i+2] << 8) | key[4*i+3]
    for i in range(nk, 4 * (nr + 1)):
        temp = w[i-1]
        if i % nk == 0:
            temp = ((temp << 8) & _M32) | (temp >> 24)
            temp = (_SBOX[(temp >> 24) & 0xFF] << 24) | (_SBOX[(temp >> 16) & 0xFF] << 16) \
                | (_SBOX[(temp >> 8) & 0xFF] << 8) | _SBOX[temp & 0xFF]
            temp ^= _RCON[i // nk - 1] << 24
        elif nk > 6 and i % nk == 4:
            temp = (_SBOX[(temp >> 24) & 0xFF] << 24) | (_SBOX[(temp >> 16) & 0xFF] << 16) \
                | (_SBOX[(temp >> 8) & 0xFF] << 8) | _SBOX[temp & 0xFF]
        w[i] = w[i - nk] ^ temp
    return w, nr


def _encrypt_block(s0, s1, s2, s3, w, nr,
                   _TE0=_TE0, _TE1=_TE1, _TE2=_TE2, _TE3=_TE3, _SBOX=_SBOX):
    """Encrypt one block: takes/returns 4 column ints (big-endian words)."""
    s0 ^= w[0]; s1 ^= w[1]; s2 ^= w[2]; s3 ^= w[3]
    for rnd in range(1, nr):
        rk = 4 * rnd
        t0 = _TE0[(s0 >> 24) & 0xFF] ^ _TE1[(s1 >> 16) & 0xFF] ^ _TE2[(s2 >> 8) & 0xFF] ^ _TE3[s3 & 0xFF] ^ w[rk]
        t1 = _TE0[(s1 >> 24) & 0xFF] ^ _TE1[(s2 >> 16) & 0xFF] ^ _TE2[(s3 >> 8) & 0xFF] ^ _TE3[s0 & 0xFF] ^ w[rk+1]
        t2 = _TE0[(s2 >> 24) & 0xFF] ^ _TE1[(s3 >> 16) & 0xFF] ^ _TE2[(s0 >> 8) & 0xFF] ^ _TE3[s1 & 0xFF] ^ w[rk+2]
        t3 = _TE0[(s3 >> 24) & 0xFF] ^ _TE1[(s0 >> 16) & 0xFF] ^ _TE2[(s1 >> 8) & 0xFF] ^ _TE3[s2 & 0xFF] ^ w[rk+3]
        s0, s1, s2, s3 = t0, t1, t2, t3
    rk = 4 * nr
    b0, b1, b2, b3 = s0, s1, s2, s3
    s0 = ((_SBOX[(b0 >> 24) & 0xFF] << 24) | (_SBOX[(b1 >> 16) & 0xFF] << 16)
          | (_SBOX[(b2 >> 8) & 0xFF] << 8) | _SBOX[b3 & 0xFF]) ^ w[rk]
    s1 = ((_SBOX[(b1 >> 24) & 0xFF] << 24) | (_SBOX[(b2 >> 16) & 0xFF] << 16)
          | (_SBOX[(b3 >> 8) & 0xFF] << 8) | _SBOX[b0 & 0xFF]) ^ w[rk+1]
    s2 = ((_SBOX[(b2 >> 24) & 0xFF] << 24) | (_SBOX[(b3 >> 16) & 0xFF] << 16)
          | (_SBOX[(b0 >> 8) & 0xFF] << 8) | _SBOX[b1 & 0xFF]) ^ w[rk+2]
    s3 = ((_SBOX[(b3 >> 24) & 0xFF] << 24) | (_SBOX[(b0 >> 16) & 0xFF] << 16)
          | (_SBOX[(b1 >> 8) & 0xFF] << 8) | _SBOX[b2 & 0xFF]) ^ w[rk+3]
    return s0, s1, s2, s3


def _inv_mix_word(x):
    """InvMixColumns applied to one 32-bit column word (equivalent inverse cipher)."""
    a0 = (x >> 24) & 0xFF; a1 = (x >> 16) & 0xFF; a2 = (x >> 8) & 0xFF; a3 = x & 0xFF
    return ((_gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)) << 24 |
            (_gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)) << 16 |
            (_gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)) << 8 |
            (_gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)))


def _decrypt_key_schedule(w, nr):
    """Equivalent inverse-cipher key schedule: middle round keys InvMixColumns'ed."""
    dw = list(w)
    for rnd in range(1, nr):
        rk = 4 * rnd
        dw[rk] = _inv_mix_word(dw[rk])
        dw[rk+1] = _inv_mix_word(dw[rk+1])
        dw[rk+2] = _inv_mix_word(dw[rk+2])
        dw[rk+3] = _inv_mix_word(dw[rk+3])
    return dw


def _decrypt_block(s0, s1, s2, s3, dw, nr,
                   _TD0=_TD0, _TD1=_TD1, _TD2=_TD2, _TD3=_TD3, _INV_SBOX=_INV_SBOX):
    rk = 4 * nr
    s0 ^= dw[rk]; s1 ^= dw[rk+1]; s2 ^= dw[rk+2]; s3 ^= dw[rk+3]
    for rnd in range(nr - 1, 0, -1):
        rk = 4 * rnd
        t0 = _TD0[(s0 >> 24) & 0xFF] ^ _TD1[(s3 >> 16) & 0xFF] ^ _TD2[(s2 >> 8) & 0xFF] ^ _TD3[s1 & 0xFF] ^ dw[rk]
        t1 = _TD0[(s1 >> 24) & 0xFF] ^ _TD1[(s0 >> 16) & 0xFF] ^ _TD2[(s3 >> 8) & 0xFF] ^ _TD3[s2 & 0xFF] ^ dw[rk+1]
        t2 = _TD0[(s2 >> 24) & 0xFF] ^ _TD1[(s1 >> 16) & 0xFF] ^ _TD2[(s0 >> 8) & 0xFF] ^ _TD3[s3 & 0xFF] ^ dw[rk+2]
        t3 = _TD0[(s3 >> 24) & 0xFF] ^ _TD1[(s2 >> 16) & 0xFF] ^ _TD2[(s1 >> 8) & 0xFF] ^ _TD3[s0 & 0xFF] ^ dw[rk+3]
        s0, s1, s2, s3 = t0, t1, t2, t3
    b0, b1, b2, b3 = s0, s1, s2, s3
    s0 = ((_INV_SBOX[(b0 >> 24) & 0xFF] << 24) | (_INV_SBOX[(b3 >> 16) & 0xFF] << 16)
          | (_INV_SBOX[(b2 >> 8) & 0xFF] << 8) | _INV_SBOX[b1 & 0xFF]) ^ dw[0]
    s1 = ((_INV_SBOX[(b1 >> 24) & 0xFF] << 24) | (_INV_SBOX[(b0 >> 16) & 0xFF] << 16)
          | (_INV_SBOX[(b3 >> 8) & 0xFF] << 8) | _INV_SBOX[b2 & 0xFF]) ^ dw[1]
    s2 = ((_INV_SBOX[(b2 >> 24) & 0xFF] << 24) | (_INV_SBOX[(b1 >> 16) & 0xFF] << 16)
          | (_INV_SBOX[(b0 >> 8) & 0xFF] << 8) | _INV_SBOX[b3 & 0xFF]) ^ dw[2]
    s3 = ((_INV_SBOX[(b3 >> 24) & 0xFF] << 24) | (_INV_SBOX[(b2 >> 16) & 0xFF] << 16)
          | (_INV_SBOX[(b1 >> 8) & 0xFF] << 8) | _INV_SBOX[b0 & 0xFF]) ^ dw[3]
    return s0, s1, s2, s3


def _block_to_ints(blk):
    """16 bytes -> tuple of 4 big-endian uint32."""
    return struct.unpack(">4I", blk)


def pkcs7_pad(data):
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def pkcs7_unpad(data):
    if not data:
        raise ValueError("empty plaintext")
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad PKCS#7 padding")
    return data[:-n]


class _PyAES256CBC:
    """Pure-Python AES-256 in CBC mode with PKCS#7 padding."""

    def __init__(self, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 requires a 32-byte key")
        if len(iv) != 16:
            raise ValueError("IV must be 16 bytes")
        self._w, self._nr = _expand_key(key)
        self._dw = _decrypt_key_schedule(self._w, self._nr)
        self._prev = _block_to_ints(iv)

    def encrypt(self, plaintext):
        data = pkcs7_pad(plaintext)
        out = bytearray(len(data))
        p0, p1, p2, p3 = self._prev
        w, nr = self._w, self._nr
        unpack_from, pack_into = _unpack_from, _pack_into
        for i in range(0, len(data), 16):
            s0, s1, s2, s3 = unpack_from(">4I", data, i)
            s0 ^= p0; s1 ^= p1; s2 ^= p2; s3 ^= p3
            p0, p1, p2, p3 = _encrypt_block(s0, s1, s2, s3, w, nr)
            pack_into(">4I", out, i, p0, p1, p2, p3)
        self._prev = (p0, p1, p2, p3)
        return bytes(out)

    def decrypt(self, ciphertext):
        if len(ciphertext) % 16:
            raise ValueError("ciphertext length not a multiple of 16")
        out = bytearray(len(ciphertext))
        p0, p1, p2, p3 = self._prev
        dw, nr = self._dw, self._nr
        unpack_from, pack_into = _unpack_from, _pack_into
        for i in range(0, len(ciphertext), 16):
            s0, s1, s2, s3 = unpack_from(">4I", ciphertext, i)
            r0, r1, r2, r3 = _decrypt_block(s0, s1, s2, s3, dw, nr)
            pack_into(">4I", out, i, r0 ^ p0, r1 ^ p1, r2 ^ p2, r3 ^ p3)
            p0, p1, p2, p3 = s0, s1, s2, s3
        self._prev = (p0, p1, p2, p3)
        return pkcs7_unpad(bytes(out))


# ---------------------------------------------------------------------------
# optional native acceleration: Windows CNG (bcrypt.dll), hardware-accelerated
# ---------------------------------------------------------------------------
_NATIVE_OK = False

try:
    if sys.platform == "win32":
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        _bcrypt = _ctypes.WinDLL("bcrypt.dll")

        _bcrypt.BCryptOpenAlgorithmProvider.restype = _wintypes.LONG
        _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
            _ctypes.POINTER(_ctypes.c_void_p), _ctypes.c_wchar_p, _ctypes.c_wchar_p, _wintypes.ULONG]
        _bcrypt.BCryptSetProperty.restype = _wintypes.LONG
        _bcrypt.BCryptSetProperty.argtypes = [
            _ctypes.c_void_p, _ctypes.c_wchar_p, _ctypes.c_void_p, _wintypes.ULONG, _wintypes.ULONG]
        _bcrypt.BCryptGetProperty.restype = _wintypes.LONG
        _bcrypt.BCryptGetProperty.argtypes = [
            _ctypes.c_void_p, _ctypes.c_wchar_p, _ctypes.c_void_p, _wintypes.ULONG,
            _ctypes.POINTER(_wintypes.ULONG), _wintypes.ULONG]
        _bcrypt.BCryptGenerateSymmetricKey.restype = _wintypes.LONG
        _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
            _ctypes.c_void_p, _ctypes.POINTER(_ctypes.c_void_p), _ctypes.c_void_p, _wintypes.ULONG,
            _ctypes.c_void_p, _wintypes.ULONG, _wintypes.ULONG]
        _bcrypt.BCryptEncrypt.restype = _wintypes.LONG
        _bcrypt.BCryptEncrypt.argtypes = [
            _ctypes.c_void_p, _ctypes.c_void_p, _wintypes.ULONG, _ctypes.c_void_p, _ctypes.c_void_p,
            _wintypes.ULONG, _ctypes.c_void_p, _wintypes.ULONG, _ctypes.POINTER(_wintypes.ULONG), _wintypes.ULONG]
        _bcrypt.BCryptDecrypt.restype = _wintypes.LONG
        _bcrypt.BCryptDecrypt.argtypes = [
            _ctypes.c_void_p, _ctypes.c_void_p, _wintypes.ULONG, _ctypes.c_void_p, _ctypes.c_void_p,
            _wintypes.ULONG, _ctypes.c_void_p, _wintypes.ULONG, _ctypes.POINTER(_wintypes.ULONG), _wintypes.ULONG]
        _bcrypt.BCryptDestroyKey.restype = _wintypes.LONG
        _bcrypt.BCryptDestroyKey.argtypes = [_ctypes.c_void_p]
        _bcrypt.BCryptCloseAlgorithmProvider.restype = _wintypes.LONG
        _bcrypt.BCryptCloseAlgorithmProvider.argtypes = [_ctypes.c_void_p, _wintypes.ULONG]

        _NATIVE_OK = True
except Exception:
    _NATIVE_OK = False


class _NativeAES256CBC:
    """AES-256-CBC (PKCS#7) via Windows CNG (bcrypt.dll) — hardware accelerated."""

    def __init__(self, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 requires a 32-byte key")
        if len(iv) != 16:
            raise ValueError("IV must be 16 bytes")
        ctypes = _ctypes
        self._alg = ctypes.c_void_p()
        if _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(self._alg), "AES", None, 0) != 0:
            raise RuntimeError("BCryptOpenAlgorithmProvider failed")
        # CBC mode must be a null-terminated UTF-16LE wide string.
        mode = ("ChainingModeCBC" + "\x00").encode("utf-16-le")
        if _bcrypt.BCryptSetProperty(self._alg, "ChainingMode",
                                     ctypes.create_string_buffer(mode), len(mode), 0) != 0:
            raise RuntimeError("BCryptSetProperty failed")
        objlen = _wintypes.ULONG()
        reslen = _wintypes.ULONG()
        if _bcrypt.BCryptGetProperty(self._alg, "ObjectLength",
                                     ctypes.byref(objlen), 4, ctypes.byref(reslen), 0) != 0:
            raise RuntimeError("BCryptGetProperty failed")
        # Keep the key-object buffer alive for the lifetime of the key handle:
        # CNG references this buffer, so it must not be garbage collected.
        self._keyobj = ctypes.create_string_buffer(objlen.value)
        self._hkey = ctypes.c_void_p()
        keybuf = ctypes.create_string_buffer(key, len(key))
        if _bcrypt.BCryptGenerateSymmetricKey(self._alg, ctypes.byref(self._hkey),
                                              self._keyobj, objlen.value, keybuf, len(key), 0) != 0:
            raise RuntimeError("BCryptGenerateSymmetricKey failed")
        self._iv = bytes(iv)

    def _crypt(self, data, encrypt):
        ctypes = _ctypes
        out = ctypes.create_string_buffer(len(data))
        ivbuf = ctypes.create_string_buffer(self._iv, 16)
        reslen = _wintypes.ULONG()
        fn = _bcrypt.BCryptEncrypt if encrypt else _bcrypt.BCryptDecrypt
        st = fn(self._hkey, ctypes.create_string_buffer(data, len(data)), len(data), None,
                ivbuf, 16, out, len(data), ctypes.byref(reslen), 0)
        if st != 0:
            raise RuntimeError("BCryptEncrypt/Decrypt failed: 0x%X" % (st & 0xFFFFFFFF))
        self._iv = ivbuf.raw[:16]
        return out.raw[:reslen.value]

    def encrypt(self, plaintext):
        return self._crypt(pkcs7_pad(plaintext), True)

    def decrypt(self, ciphertext):
        if len(ciphertext) % 16:
            raise ValueError("ciphertext length not a multiple of 16")
        return pkcs7_unpad(self._crypt(ciphertext, False))

    def __del__(self):
        try:
            if getattr(self, "_hkey", None):
                _bcrypt.BCryptDestroyKey(self._hkey)
            if getattr(self, "_alg", None):
                _bcrypt.BCryptCloseAlgorithmProvider(self._alg, 0)
        except Exception:
            pass


AES256CBC = _NativeAES256CBC if _NATIVE_OK else _PyAES256CBC
