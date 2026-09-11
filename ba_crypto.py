# -*- coding: utf-8 -*-
"""Broken Arrow DataBaseCompiled format: decrypt/encrypt + UABEA dump I/O.

The asset 'DataBaseCompiled' inside data.unity3d is a serialized object whose
JSON dump (UABEA "Export Dump") contains one base64 field per database table:

    {
      "m_GameObject": {...}, "m_Enabled": 1, "m_Script": {...},
      "m_Name": "DataBaseCompiled",
      "Units": "ZmhrM3...", "AbilitiesJson": "...", ..., "Level": 2
    }

Each table field is  base64( marker "fhk3s0g3" (8B) + IV (16B) + AES-256-CBC ),
key = ASCII '09234237536700238099172758697347', PKCS#7 padding.
The plaintext is a compact JSON array (Unity JsonUtility style).
"""

import base64
import json
import os

from ba_aes import AES256CBC

KEY = b"09234237536700238099172758697347"
MARKER = b"fhk3s0g3"
_MARKER_B64_PREFIX = base64.b64encode(MARKER).decode("ascii")[:8]

# Field name in the dump  ->  file/table name (without extension).
# 'Units' is the only field without the 'Json' suffix.
TABLE_FIELDS = [
    "Units", "AbilitiesJson", "UnitAbilitiesJson", "AmmunitionsJson",
    "ArmorsJson", "MobilityJson", "FlyPresetsJson", "UnitPropulsionsJson",
    "CountriesJson", "TurretsJson", "TurretUnitsJson", "WeaponsJson",
    "TurretWeaponsJson", "WeaponAmmunitionsJson", "SensorUnitsJson",
    "SensorsJson", "SquadMembersJson", "SquadWeaponsJson",
    "ModificationsJson", "OptionsJson", "UnitArmorsJson",
    "SpecializationAvailabilitiesJson", "SpecializationsJson",
    "TransportAvailabilitiesJson",
]

def table_name(field):
    """Map a dump field name to a logical table name.

    'Units' stays 'Units'; every other field name ends with 'Json' and maps
    to the table name with that suffix stripped.
    """
    if field == "Units":
        return "Units"
    return field[:-4] if field.endswith("Json") else field

# ---------------------------------------------------------------------------
# primitive crypto
# ---------------------------------------------------------------------------

def decrypt_field(value):
    """Decrypt one base64 field. Returns plaintext str. Raises on bad data."""
    blob = base64.b64decode(value)
    if len(blob) < 24 or blob[:8] != MARKER:
        raise ValueError("bad marker (not a Broken Arrow database field)")
    iv, ct = blob[8:24], blob[24:]
    return AES256CBC(KEY, iv).decrypt(ct).decode("utf-8")


def encrypt_field(plaintext):
    """Encrypt plaintext str into a base64 field (fresh random IV)."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    iv = os.urandom(16)
    ct = AES256CBC(KEY, iv).encrypt(plaintext)
    return base64.b64encode(MARKER + iv + ct).decode("ascii")


# ---------------------------------------------------------------------------
# dump file I/O (byte-compatible with the UABEA export format)
# ---------------------------------------------------------------------------

def load_dump(path):
    """Parse a DataBaseCompiled dump JSON, preserving key order."""
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _dump_text(obj):
    """Serialize like UABEA's export dump: CRLF, 2-space indent, no trailing NL."""
    return json.dumps(obj, indent=2, ensure_ascii=False).replace("\n", "\r\n")


def write_dump(path, obj):
    """Write a dump JSON in UABEA's byte format (CRLF, no trailing newline)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(_dump_text(obj))


def is_dump_obj(obj):
    """Heuristic: does this JSON look like a DataBaseCompiled dump object?"""
    return isinstance(obj, dict) and ("m_Name" in obj or any(k in obj for k in TABLE_FIELDS))


# ---------------------------------------------------------------------------
# table extraction / injection
# ---------------------------------------------------------------------------

def decrypt_tables(obj, progress=None):
    """obj: parsed dump. Returns {table_name: parsed JSON value}."""
    tables = {}
    errors = []
    for key, value in obj.items():
        if not isinstance(value, str) or not value.startswith(_MARKER_B64_PREFIX):
            continue
        name = table_name(key)
        if progress:
            progress(name)
        try:
            plain = decrypt_field(value)
            tables[name] = json.loads(plain)
        except Exception as e:  # noqa: BLE001 - report and continue
            errors.append((key, str(e)))
    return tables, errors


def encrypt_tables(obj, tables, progress=None):
    """obj: parsed dump (template). tables: {name: value}. Returns new dump obj."""
    out = json.loads(json.dumps(obj))  # deep copy, key order kept
    for key in list(out.keys()):
        if not isinstance(out[key], str) or not out[key].startswith(_MARKER_B64_PREFIX):
            continue
        name = table_name(key)
        match = next((t for t in tables if t.lower() == name.lower()), None)
        if match is not None:
            if progress:
                progress(match)
            out[key] = encrypt_field(json.dumps(tables[match], ensure_ascii=False,
                                                separators=(",", ":")))
    return out


# ---------------------------------------------------------------------------
# folder export / import (Release-style: one .json per table)
# ---------------------------------------------------------------------------

def export_folder(obj, folder, progress=None):
    """Decrypt all tables and write pretty-printed <Table>.json into folder."""
    os.makedirs(folder, exist_ok=True)
    tables, errors = decrypt_tables(obj, progress=progress)
    written = 0
    for name, value in tables.items():
        path = os.path.join(folder, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
        written += 1
    return tables, errors, written


def import_folder(folder, progress=None):
    """Read <Table>.json files, return (tables, missing)."""
    tables = {}
    missing = []
    for name in [table_name(k) for k in TABLE_FIELDS]:
        path = os.path.join(folder, name + ".json")
        if not os.path.isfile(path):
            missing.append(name)
            continue
        if progress:
            progress(name)
        with open(path, "r", encoding="utf-8-sig") as f:
            tables[name] = json.load(f)
    return tables, missing
