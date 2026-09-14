# -*- coding: utf-8 -*-
"""Broken Arrow catalog.json (Addressables ContentCatalogData) parser/serializer.

Format fully reverse-engineered from com.unity.addressables ContentCatalogData.cs +
SerializationUtilities.cs (ObjectType byte-tagged objects, little-endian int32)."""
import json, base64, struct, os

def ri32(d, o):
    return d[o] | (d[o+1]<<8) | (d[o+2]<<16) | (d[o+3]<<24)

def wi32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)

def read_object(d, o):
    t = d[o]; o += 1
    if t == 0:   # AsciiString
        ln = ri32(d, o); o += 4
        return (t, d[o:o+ln].decode("ascii")), o+ln
    if t == 1:   # UnicodeString
        ln = ri32(d, o); o += 4
        return (t, d[o:o+ln].decode("utf-16-le")), o+ln
    if t == 2:   # UInt16
        return (t, d[o] | (d[o+1]<<8)), o+2
    if t == 3:   # UInt32
        return (t, ri32(d, o)), o+4
    if t == 4:   # Int32
        v = ri32(d, o); return (t, v-0x100000000 if v>=0x80000000 else v), o+4
    if t == 5:   # Hash128
        ln = d[o]; o += 1; return (t, d[o:o+ln].decode("ascii")), o+ln
    if t == 6:   # Type
        ln = d[o]; o += 1; return (t, d[o:o+ln].decode("ascii")), o+ln
    if t == 7:   # JsonObject
        asmlen = d[o]; o += 1
        asm = d[o:o+asmlen].decode("ascii"); o += asmlen
        clen = d[o]; o += 1
        cls = d[o:o+clen].decode("ascii"); o += clen
        jlen = ri32(d, o); o += 4
        jtext = d[o:o+jlen].decode("utf-16-le"); o += jlen
        return (t, (asm, cls, jtext)), o
    raise ValueError(f"bad object type {t} at offset {o-1}")

def write_object(buf, typ, val):
    buf.append(typ)
    if typ == 0:
        b = val.encode("ascii"); buf += wi32(len(b)); buf += b
    elif typ == 1:
        b = val.encode("utf-16-le"); buf += wi32(len(b)); buf += b
    elif typ == 2:
        buf += struct.pack("<H", val)
    elif typ == 3:
        buf += struct.pack("<I", val)
    elif typ == 4:
        buf += struct.pack("<i", val)
    elif typ in (5, 6):
        b = val.encode("ascii"); buf.append(len(b)); buf += b
    elif typ == 7:
        asm, cls, jtext = val
        ba = asm.encode("ascii"); buf.append(len(ba)); buf += ba
        bc = cls.encode("ascii"); buf.append(len(bc)); buf += bc
        bj = jtext.encode("utf-16-le"); buf += wi32(len(bj)); buf += bj
    else:
        raise ValueError(f"bad write type {typ}")

class Catalog:
    def __init__(self, path):
        self.path = path
        self.cat = json.load(open(path, encoding="utf-8"))
        self._parse()

    def _parse(self):
        c = self.cat
        br = base64.b64decode(c["m_BucketDataString"])
        n = ri32(br, 0)
        self.buckets = []
        o = 4
        for _ in range(n):
            dataOffset = ri32(br, o); o += 4
            ec = ri32(br, o); o += 4
            ent = [ri32(br, o+k*4) for k in range(ec)]
            o += ec*4
            self.buckets.append({"dataOffset": dataOffset, "entries": ent})
        assert o == len(br), (o, len(br))

        kr = base64.b64decode(c["m_KeyDataString"])
        assert ri32(kr, 0) == n
        self.keys = []
        for b in self.buckets:
            obj, _ = read_object(kr, b["dataOffset"])
            self.keys.append(obj)

        er = base64.b64decode(c["m_EntryDataString"])
        cnt = ri32(er, 0)
        self.entries = []
        for i in range(cnt):
            idx = 4 + i*28
            self.entries.append(tuple(ri32(er, idx+k*4) for k in range(7)))
        assert 4 + cnt*28 == len(er)

        self.extra = base64.b64decode(c["m_ExtraDataString"])

    def serialize(self):
        c = json.loads(json.dumps(self.cat))
        # buckets + keys
        buf = bytearray(wi32(len(self.buckets)))
        keybuf = bytearray(wi32(len(self.keys)))
        new_buckets = []
        for b in self.buckets:
            pass
        # keys sequentially, record offsets
        key_offsets = []
        for (typ, val) in self.keys:
            key_offsets.append(len(keybuf))
            write_object(keybuf, typ, val)
        for b, koff in zip(self.buckets, key_offsets):
            bb = bytearray(wi32(koff))
            bb += wi32(len(b["entries"]))
            for e in b["entries"]:
                bb += wi32(e)
            buf += bb
        c["m_KeyDataString"] = base64.b64encode(bytes(keybuf)).decode()
        c["m_BucketDataString"] = base64.b64encode(bytes(buf)).decode()
        # entries
        eb = bytearray(wi32(len(self.entries)))
        for e in self.entries:
            for v in e:
                eb += wi32(v)
        c["m_EntryDataString"] = base64.b64encode(bytes(eb)).decode()
        c["m_ExtraDataString"] = base64.b64encode(self.extra).decode()
        return c

    def save(self, outpath):
        r"""写 catalog.json。

        ⛔ v1.8.97 修的真 bug：以前**直接 open(outpath,"w") 覆盖**——而 outpath 经常就是
           **游戏本体那份 catalog.json**（`update_crc.update_catalog` / `import_pack` 都会调它）。
           写到一半崩 / 磁盘满 / Ctrl+C ⇒ 原文件被截断 ⇒ **游戏读不了 catalog，整个资源系统瘫掉** ✗
           现在改成：先写 `outpath + ".tmp"` → **自检能重新解析** → `os.replace` 原子替换。
           （`.tmp` 与目标同目录是必须的：跨盘 replace 不是原子的。）
        """
        c = self.serialize()
        tmp = outpath + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(c, f, ensure_ascii=False)
        # 自检：新文件必须能被自己重新解析（校验桶/键/条目/extra 的偏移一致）
        try:
            chk = Catalog(tmp)
            if len(chk.entries) != len(self.entries):
                raise ValueError("条目数不一致：写出 %d，内存 %d"
                                 % (len(chk.entries), len(self.entries)))
        except Exception as e:                                    # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError("写出的 catalog 自检失败，**原文件未动**：%s" % e)
        os.replace(tmp, outpath)
        return outpath

if __name__ == "__main__":
    CAT = r"<游戏安装目录>\BrokenArrow_Data\StreamingAssets\aa\catalog.json"
    cat = Catalog(CAT)
    print("buckets:", len(cat.buckets), "keys:", len(cat.keys), "entries:", len(cat.entries))
    # round-trip: serialize and compare each string field to original
    c2 = cat.serialize()
    for f in ["m_KeyDataString","m_BucketDataString","m_EntryDataString","m_ExtraDataString"]:
        same = (c2[f] == cat.cat[f])
        print(f"  {f}: roundtrip identical = {same}")
    # also verify by re-parsing serialized output
    import tempfile, os
    tmp = os.path.join(os.environ.get("TEMP","."), "cat_roundtrip.json")
    cat.save(tmp)
    cat3 = Catalog(tmp)
    print("reparse buckets/keys/entries:", len(cat3.buckets), len(cat3.keys), len(cat3.entries))
    print("reparse keys[0]:", cat3.keys[0][0], repr(cat3.keys[0][1])[:40])
    print("reparse RU_BMPT key idx:", [i for i,(t,v) in enumerate(cat3.keys) if t==0 and v=="RU_BMPT"][0])
