# -*- coding: utf-8 -*-
r"""断箭（Broken Arrow）bundle CRC 计算器 —— 已破解算法，不依赖 Unity。

算法（逆向得出）：
  Unity 的 AssetBundle CRC = 对「解压后的数据」算标准 CRC-32（zlib.crc32）。
  解压后数据 = 所有数据块（lz4/lzma）解压后按顺序拼接。

用法:
    python compute_bundle_crc.py <bundle文件>
    python compute_bundle_crc.py --dir <aa/PC目录>
    python compute_bundle_crc.py --update-catalog <catalog.json>
"""
import sys, struct, os, zlib, glob, re
import lz4.block, lzma


def decomp_block(comp, usize, flag):
    """按块 flag 解压一个数据块。"""
    ct = flag & 0x3F
    if ct == 0:
        return comp
    if ct == 1:  # lzma
        props, dsize = struct.unpack("<BI", comp[:5])
        lc = props % 9; rem = props // 9; pb = rem // 5; lp = rem % 5
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dsize, "lc": lc, "lp": lp, "pb": pb}])
        return dec.decompress(comp[5:])
    if ct in (2, 3):  # lz4 / lz4hc
        return lz4.block.decompress(comp, uncompressed_size=usize)
    raise ValueError("未知压缩类型 %d" % ct)


def compute_bundle_crc(path):
    """流式计算一个 bundle 的 CRC（内存占用小，支持 3.4GB 大 bundle）。"""
    f = open(path, "rb")
    f.seek(8 + 4)  # 跳过 signature + format version
    def read_cstr():
        b = b""
        while True:
            ch = f.read(1)
            if ch == b"\x00":
                break
            b += ch
        return b
    read_cstr()  # unity version
    read_cstr()  # unity revision
    size = struct.unpack(">Q", f.read(8))[0]
    cbsize = struct.unpack(">I", f.read(4))[0]
    ubsize = struct.unpack(">I", f.read(4))[0]
    flags = struct.unpack(">I", f.read(4))[0]
    a = (f.tell() + 15) // 16 * 16  # header 对齐
    # block info
    if flags & 0x80:  # BlocksInfoAtTheEnd
        f.seek(size - cbsize)
        bi_comp = f.read(cbsize)
    else:  # BlocksAndDirectoryInfoCombined
        f.seek(a)
        bi_comp = f.read(cbsize)
    bi = decomp_block(bi_comp, ubsize, flags)
    p = 16  # 跳过 uncompressedDataHash
    nb = struct.unpack(">i", bi[p:p+4])[0]; p += 4
    blocks = []
    for _ in range(nb):
        usize = struct.unpack(">I", bi[p:p+4])[0]; p += 4
        csize = struct.unpack(">I", bi[p:p+4])[0]; p += 4
        bflag = struct.unpack(">H", bi[p:p+2])[0]; p += 2
        blocks.append((usize, csize, bflag))
    # 数据块起始位置
    pos = a if flags & 0x80 else a + cbsize
    if flags & 0x200:  # BlockInfoNeedPaddingAtStart
        pos = (pos + 15) // 16 * 16
    # 流式解压 + CRC
    crc = 0
    for usize, csize, bflag in blocks:
        f.seek(pos)
        comp = f.read(csize)
        pos += csize
        dec = comp if csize == usize else decomp_block(comp, usize, bflag)
        crc = zlib.crc32(dec, crc)
    f.close()
    return crc & 0xFFFFFFFF


def compute_dir(dirpath):
    """算目录下所有 bundle 的 CRC，返回 {hash: crc}。"""
    result = {}
    for f in sorted(glob.glob(os.path.join(dirpath, "*.bundle"))):
        m = re.search(r"([0-9a-fA-F]{32})[.]bundle", os.path.basename(f))
        if not m:
            continue
        hashv = m.group(1).lower()
        result[hashv] = compute_bundle_crc(f)
        print("CRC %u  %s" % (result[hashv], os.path.basename(f)))
    return result


def update_catalog(catalog_path):
    """算所有 bundle CRC 并写回 catalog.json（替代 Unity 脚本）。

    v1.6.0 起委托 update_crc.update_catalog（catalog_mod 正确重建 ExtraData，
    消除旧的临时解析实现——两处实现已合并为一处）。
    """
    from update_crc import update_catalog as _update
    pc_dir = os.path.join(os.path.dirname(os.path.abspath(catalog_path)), "PC")
    print("算 CRC：", pc_dir)
    crc_map = compute_dir(pc_dir)
    if not crc_map:
        print("没算到任何 CRC"); return 0
    updated = _update(catalog_path, crc_map)
    if updated:
        for h, old, new in updated:
            print(f"  {h}: {old} -> {new}")
        print("已更新 %d 个 bundle 的 CRC，写回 catalog.json" % len(updated))
    else:
        print("没有匹配到需要更新的 bundle（CRC 已是最新，或文件名 hash 没对上）")
    return len(updated)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--dir":
        compute_dir(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--update-catalog":
        update_catalog(sys.argv[2])
    elif len(sys.argv) >= 2:
        print("CRC =", compute_bundle_crc(sys.argv[1]))
    else:
        print(__doc__)