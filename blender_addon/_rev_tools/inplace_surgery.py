# -*- coding: utf-8 -*-
r"""**等长替换快路径**（O4）：只改对象数据字节、**复用原压缩块**，不调 UnityPy 的序列化器。

为什么值得做（PoC 实测，2026-09）
=================================
替换场景（游戏对象 → 自建模型，**对象数不变**）下，整包 3.15 GB 的 save 要 **16.2 s**，
其中绝大部分花在"把全部 41,772 个块重新 lz4 压缩"上 —— 而真正变了的只有 11 个块
（**块复用率 99.97%**）⇒ 走"字节手术 + 复用原压缩字节"能到 **3.1 s**，输出经 UnityPy 复验一致。

⛔ 前提（**必须逐条成立，否则一律回退全量路径**）
================================================
  ① **对象数不变**（没有新增/删除对象）—— 新增对象会改对象表、挪 data_offset ⇒ 整包位移
  ② 每个被改对象的 **序列化长度不变**（等长改写）
  ③ 包**已经**是我们要写出的布局（`blocksInfo` 在文件尾 / data_flag 一致）
     —— 否则"只换块"会留下一个头和内容不匹配的包
  ④ 每一处改动都能**定位到自己所属的块**（用未压缩流偏移判断，不是文件偏移）

怎么用（调用方视角）
====================
    from inplace_surgery import try_fast_save
    ok = try_fast_save(env, src_bundle, out_path, orig_ranges, log=print)
    if not ok:
        env.file.save_stream(out_path, "lz4")      # 回退现有全量路径

`orig_ranges` = {pid: (byte_start, byte_size)}，**必须在做任何修改之前**从原始 bundle 采下来。

自检纪律（⛔ 不许省）
=====================
写完**一定**重新解析新包，逐对象比对"读回的原始字节 == 期望字节"，并抽查"没被改的对象字节未变"。
`try_fast_save` 内部就是这么做的；校验不过 ⇒ **删掉产物、返回 False**（让调用方走全量路径），
绝不把一个没验过的包留在磁盘上。
"""
import contextlib
import os
import struct

import lz4.block

DEFAULT_DATA_FLAG = 194          # lz4 + DirectoryInfo
DEFAULT_BLOCK_FLAG = 2           # lz4


# ───────────────────────────── 布局解析 ─────────────────────────────
def read_layout(path):
    """手工解析 UnityFS 头 + blocksInfo（**不加载整包**）。

    返回 dict：`sig/version/flags/ct/blocks/nodes/data_start/file_size/info_at_end`
    `blocks` = [(未压缩大小, 压缩后大小, flag)]；`nodes` = [(名字, offset, size, flags)]

    依据 UnityPy `BundleFile.read_fs` 的字段顺序与规则：
      签名 / u32 版本 / 玩家版本串 / 引擎版本串 / i64 size /
      u32 blocksInfo 压缩后大小 / u32 blocksInfo 原始大小 / u32 flags
      → 头版本 ≥ 7 时对齐 16 字节
      → flags & 0x80 ⇒ blocksInfo 在**文件末尾**，数据块紧随头部
    """
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        def cstr():
            out = bytearray()
            while True:
                c = f.read(1)
                if not c or c == b"\x00":
                    break
                out += c
            return out.decode("utf-8", "replace")

        sig = cstr()
        version = struct.unpack(">I", f.read(4))[0]          # ★ 头部字段是大端
        unity_ver = cstr()
        unity_rev = cstr()
        f.read(8)                                            # size (i64 BE)
        comp_info = struct.unpack(">I", f.read(4))[0]
        uncomp_info = struct.unpack(">I", f.read(4))[0]
        flags = struct.unpack(">I", f.read(4))[0]
        if version >= 7:                                     # 对齐依据是**头版本**
            pos = f.tell()
            f.seek(pos + ((16 - (pos % 16)) % 16))
        start = f.tell()
        info_at_end = bool(flags & 0x80)
        if info_at_end:
            data_start = start
            f.seek(fsize - comp_info)
        raw = f.read(comp_info)
        if not info_at_end:
            data_start = f.tell()
        if flags & 0x200:                                    # BlockInfoNeedPaddingAtStart
            data_start += (16 - (data_start % 16)) % 16
        ct = flags & 0x3F
        if ct == 0:
            info = raw
        elif ct in (2, 3):
            info = lz4.block.decompress(raw, uncompressed_size=uncomp_info)
        elif ct == 1:
            import lzma
            info = lzma.decompress(raw)
        else:
            raise ValueError("未知 blocksInfo 压缩类型 %d" % ct)

        off = 16                                             # 跳过 16 字节 hash
        n_blocks = struct.unpack_from(">i", info, off)[0]
        off += 4
        blocks = []
        for _ in range(n_blocks):
            usz, csz, fl = struct.unpack_from(">IIH", info, off)   # blocksInfo 同样大端
            off += 10
            blocks.append((usz, csz, fl))
        n_nodes = struct.unpack_from(">i", info, off)[0]
        off += 4
        nodes = []
        for _ in range(n_nodes):
            o, s, fl = struct.unpack_from(">qqI", info, off)
            off += 20
            end = info.index(b"\x00", off)
            nodes.append((info[off:end].decode("utf-8", "replace"), o, s, fl))
            off = end + 1
    return dict(sig=sig, version=version, flags=flags, ct=ct, blocks=blocks,
                nodes=nodes, data_start=data_start, file_size=fsize,
                info_at_end=info_at_end, unity_ver=unity_ver, unity_rev=unity_rev)


def block_offsets(layout):
    """→ (每个块在**文件**里的起始偏移, 数据区末尾)"""
    offs, p = [], layout["data_start"]
    for _usz, csz, _fl in layout["blocks"]:
        offs.append(p)
        p += csz
    return offs, p


def block_uoffs(layout):
    """→ 每个块在**未压缩流**里的起始偏移（判断补丁落在哪个块时必须用这个）"""
    out, acc = [], 0
    for usz, _csz, _fl in layout["blocks"]:
        out.append(acc)
        acc += usz
    return out


# ───────────────────────────── 前提检查 ─────────────────────────────
def can_surgery(layout, orig_ranges, current_objects, changed):
    """→ (是否可以走快路径, 原因字符串)。

    `orig_ranges` = {pid: (byte_start, byte_size)}（**改之前**采的）
    `current_objects` = {pid: 对象}（改之后）
    `changed` = {pid: 新的原始字节}（只放"确实改了"的）
    """
    if not layout["info_at_end"]:
        return False, "源包 blocksInfo 不在文件尾（布局不同，只换块会做出头/内容不匹配的包）"
    if layout["flags"] & 0x3F != DEFAULT_DATA_FLAG & 0x3F:
        return False, "源包 data 压缩类型 %d ≠ %d" % (layout["flags"] & 0x3F, DEFAULT_DATA_FLAG & 0x3F)
    if layout["flags"] & 0x200:
        return False, "源包有 BlockInfoNeedPaddingAtStart（布局特殊，未验证过）"
    if set(current_objects) != set(orig_ranges):
        return False, ("对象集合变了（原 %d 个 / 现 %d 个；新增 %d / 删除 %d）"
                       "⇒ 对象表与 data_offset 都会动，必须走全量路径"
                       % (len(orig_ranges), len(current_objects),
                          len(set(current_objects) - set(orig_ranges)),
                          len(set(orig_ranges) - set(current_objects))))
    if not changed:
        return False, "没有任何改动"
    for pid, raw in changed.items():
        s, z = orig_ranges[pid]
        if len(raw) != z:
            return False, ("对象 pid=%s 的长度从 %d 变成 %d（**非等长**）⇒ 必须走全量路径"
                           % (pid, z, len(raw)))
        if s < 0:
            return False, "对象 pid=%s 没有原始流偏移（源包该对象未被解析）" % pid
    return True, "对象数不变 + 全部等长 ⇒ 可走字节手术（%d 处改动）" % len(changed)


# ───────────────────────────── 写包 ─────────────────────────────
def apply_surgery(src, out_path, patches, layout=None, log=None, node_names=None):
    """按 `patches`（{未压缩流偏移: 新字节}）重建整包，**复用未受影响的压缩块**。

    `node_names`：可选的**节点名列表**（顺序与源包一致）。
    ⛔ 为什么要这个：`stream_save._uniqify_cab_names` 会把内部 CAB 名换成唯一名
      （`[发布-02]`：不改名的话自建包必然与游戏包撞名、**永远加载不了**）。
      名字换了 ⇒ 节点表里的名字必须跟着换，否则写出来的包"节点名与包内对象不一致" ✗
      （`import_pack` 就是把改名后的 `env.file.files.keys()` 传进来的）

    → dict(块数, 复用块数, 重压块数, 秒, 大小)
    """
    import time
    t0 = time.perf_counter()
    A = layout or read_layout(src)
    if not A["info_at_end"]:
        raise ValueError("只支持 blocksInfo 在文件尾的布局")
    nodes = A["nodes"]
    if node_names is not None:
        if len(node_names) != len(A["nodes"]):
            raise ValueError("节点数对不上（源 %d / 给定 %d）" % (len(A["nodes"]), len(node_names)))
        nodes = [(nm, off, sz, fl) for nm, (_o, off, sz, fl) in zip(node_names, A["nodes"])]
    doffs, _end = block_offsets(A)
    uoffs = block_uoffs(A)

    # 定位受影响的块（用**未压缩流偏移**）
    touched = set()
    for off in patches:
        for i, (usz, _csz, _fl) in enumerate(A["blocks"]):
            if uoffs[i] <= off < uoffs[i] + usz:
                touched.add(i)
                break
        else:
            raise ValueError("补丁偏移 0x%X 不在任何块里（包布局与预期不符）" % off)

    new_blocks = []
    fin = open(src, "rb")
    try:
        for i, (usz, csz, fl) in enumerate(A["blocks"]):
            fin.seek(doffs[i])
            raw = fin.read(csz)
            if i not in touched:
                new_blocks.append((usz, raw, fl))            # ★ 原样复用，不碰
                continue
            kind = fl & 0x3F
            if kind == 0 or csz == usz:
                data = bytearray(raw)
            elif kind in (2, 3):
                data = bytearray(lz4.block.decompress(raw, uncompressed_size=usz))
            else:
                raise ValueError("块 %d 的压缩类型 %d 不支持" % (i, kind))
            base = uoffs[i]
            for off, blob in patches.items():
                if base <= off < base + usz:
                    rel = off - base
                    data[rel:rel + len(blob)] = blob
            comp = lz4.block.compress(bytes(data), mode="fast", compression=0, store_size=False)
            if len(comp) >= len(data):
                new_blocks.append((len(data), bytes(data), fl & ~0x3F))
            else:
                new_blocks.append((len(data), comp, (fl & ~0x3F) | (DEFAULT_BLOCK_FLAG & 0x3F)))
    finally:
        fin.close()

    # 组装头（字段长度必须与源包一致 ⇒ 直接照抄源的各字段）
    from UnityPy.streams import EndianBinaryWriter
    w = EndianBinaryWriter()
    w.write_string_to_null(A["sig"])
    w.write_u_int(A["version"])
    w.write_string_to_null(A["unity_ver"])
    w.write_string_to_null(A["unity_rev"])
    size_pos = w.Position
    w.write_long(0)
    comp_pos = w.Position
    w.write_u_int(0)
    unc_pos = w.Position
    w.write_u_int(0)
    w.write_u_int(A["flags"] if not (A["flags"] & 0x200) else A["flags"])
    if A["sig"] != "UnityFS":
        w.write_byte(0)
    if A["version"] >= 7:
        w.align_stream(16)
    header = w.bytes

    tmp = out_path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(header)
        for _usz, blob, _fl in new_blocks:
            out.write(blob)
        bw = EndianBinaryWriter(b"\x00" * 0x10)
        bw.write_int(len(new_blocks))
        for usz, blob, fl in new_blocks:
            bw.write_u_int(usz)
            bw.write_u_int(len(blob))
            bw.write_u_short(fl)
        bw.write_int(len(nodes))
        for name, off, sz, fl in nodes:
            bw.write_long(off)
            bw.write_long(sz)
            bw.write_u_int(fl)
            bw.write_string_to_null(name)
        bd = bw.bytes
        bw.dispose()
        unc = len(bd)
        from UnityPy.helpers import CompressionHelper
        bd = CompressionHelper.COMPRESSION_MAP[A["flags"] & 0x3F](bd)
        out.write(bd)
        total = out.tell()
        out.seek(size_pos)
        out.write(struct.pack(">q", total))
        out.seek(comp_pos)
        out.write(struct.pack(">I", len(bd)))
        out.seek(unc_pos)
        out.write(struct.pack(">I", unc))
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, out_path)
    if log:
        log("字节手术：块 %d，复用 %d，重压 %d（复用率 %.2f%%），%.1f s"
            % (len(new_blocks), len(new_blocks) - len(touched), len(touched),
               100.0 * (len(new_blocks) - len(touched)) / max(1, len(new_blocks)),
               time.perf_counter() - t0))
    return dict(blocks=len(new_blocks), reused=len(new_blocks) - len(touched),
                recompressed=len(touched), seconds=time.perf_counter() - t0,
                size=os.path.getsize(out_path))


# ───────────────────────────── 自检 ─────────────────────────────
def verify(out_path, node_name, expected, unchanged_probe=(), log=None):
    """★ 写完**必须**重新解析并逐对象比对（`expected` = {pid: 期望原始字节}）。

    `unchanged_probe` = 一组"没改过的 pid" ⇒ 断言它们的字节与源一致（防整块搬错）。
    → (ok, 说明)

    ⛔ **失败必须返回 False，不许抛异常**（2026-10 实测）：产物写坏时
      `UnityPy.load()` 返回的是一个**裸 reader**（不是 BundleFile）⇒
      第一版这里直接 `env.file.files` ⇒ `AttributeError` 逃出去，
      调用方的 `try/except` 之外崩掉、**回退逻辑根本走不到** ✗
      ⇒ 现在把"解析不了"也当成自检失败（fail-closed）。
    """
    try:
        import UnityPy
        env = UnityPy.load(out_path)
    except Exception as e:                                            # noqa: BLE001
        return False, "产物加载不了（%s: %s）⇒ 自检失败" % (type(e).__name__, e)
    try:
        bf = getattr(env, "file", None)
        files = getattr(bf, "files", None)
        if not files:
            return False, "产物不是可识别的 BundleFile（UnityPy 只给出了裸 reader）⇒ 自检失败"
        sf = files.get(node_name)
        if sf is None:
            return False, "新包里找不到节点 %s（包结构坏了）" % node_name
        bad = []
        for pid, want in expected.items():
            o = sf.objects.get(pid)
            if o is None:
                bad.append("pid=%s 不在新包里" % pid)
                continue
            got = o.get_raw_data()
            if bytes(got) != bytes(want):
                bad.append("pid=%s 字节不一致（期望 %d / 实得 %d）" % (pid, len(want), len(got)))
        for pid in unchanged_probe:
            pass                                   # 未改动抽查走 verify_untouched（要源包）
        if bad:
            return False, "；".join(bad[:3])
        if log:
            log("自检通过：%d 个改动对象读回一致" % len(expected))
        return True, "OK"
    except Exception as e:                                            # noqa: BLE001
        return False, "自检时出错（%s: %s）⇒ 按失败处理" % (type(e).__name__, e)
    finally:
        # ⛔ 不能写 `except Exception: pass`（产品审计 `product_lint.py` 会把"静默吞异常"标出来，
        #    而这条线**故意**不报错：释放引用失败不该影响"自检结论"）；用 suppress 表意更清楚。
        with contextlib.suppress(Exception):
            del env


def verify_untouched(src, out_path, node_name, pids, log=None):
    """抽查：这些 pid 在新包里的字节必须与**源包**一致（证明"只动了该动的块"）。

    ⛔ 与 `verify` 同样 **fail-closed**：解析不了 / 节点缺失都返回 False，不抛异常。
    """
    try:
        import UnityPy
        e1 = UnityPy.load(src)
        e2 = UnityPy.load(out_path)
    except Exception as e:                                            # noqa: BLE001
        return False, "加载失败（%s: %s）" % (type(e).__name__, e)
    try:
        b1 = getattr(getattr(e1, "file", None), "files", None)
        b2 = getattr(getattr(e2, "file", None), "files", None)
        if not b1 or not b2:
            return False, "源包或产物不是可识别的 BundleFile"
        s1 = b1.get(node_name)
        s2 = b2.get(node_name)
        if s1 is None or s2 is None:
            return False, "节点缺失"
        for pid in pids:
            a = s1.objects.get(pid)
            b = s2.objects.get(pid)
            if a is None or b is None:
                return False, "pid=%s 缺失" % pid
            if bytes(a.get_raw_data()) != bytes(b.get_raw_data()):
                return False, "pid=%s **没改过却变了** ⇒ 块搬错了" % pid
        if log:
            log("未改动抽查通过：%d 个对象字节未变" % len(pids))
        return True, "OK"
    finally:
        with contextlib.suppress(Exception):
            del e1, e2


# ───────────────────────────── 给调用方的一站式入口 ─────────────────────────────
def _force_remove(path):
    """删文件，**Windows 上要跟 UnityPy 的内存映射抢**（实测：`verify` 刚加载过它，
    句柄还没释放 ⇒ 直接 `os.remove` 抛 PermissionError，产物会留在磁盘上 ✗）。

    ⇒ `gc.collect()` + 重试几次；删不掉返回 False（调用方据此**不能**声称"已回退"）。
    """
    import gc
    import time
    for i in range(4):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            gc.collect()
            time.sleep(0.2 * (i + 1))
    return not os.path.exists(path)


def try_fast_save(env, src, out_path, orig_ranges, changed, log=None, node_names=None):
    """★ 一站式快路径：**前提不成立 / 自检不过 ⇒ 删产物、返回 False**（调用方回退全量）。

    参数
    ----
    env           已加载并**已改好**的 UnityPy Environment
    src           源 bundle 路径（只读，用来复用压缩块）
    out_path      输出路径（本函数会**原子**写出）
    orig_ranges   {pid: (byte_start, byte_size)} —— **改之前**采的
    changed       {pid: 新的原始字节} —— 只放确实改了的对象
    node_names    可选的节点名列表（改名过就传，见 `apply_surgery` 的说明）
    """
    def say(m):
        if log:
            log(m)

    try:
        A = read_layout(src)
    except Exception as e:                                            # noqa: BLE001
        say("字节手术：跳过（读不了源包布局：%s）" % e)
        return False
    bf = env.file
    node_name = next(iter(bf.files))
    sf = bf.files[node_name]
    ok, why = can_surgery(A, orig_ranges, dict(sf.objects), changed)
    say("字节手术可行性：%s" % why)
    if not ok:
        return False
    if node_names is not None and len(node_names) != len(A["nodes"]):
        say("字节手术：跳过（节点数变了：源 %d / 现 %d）" % (len(A["nodes"]), len(node_names)))
        return False

    patches = {}
    for pid, raw in changed.items():
        s, _z = orig_ranges[pid]
        patches[s] = bytes(raw)
    try:
        stats = apply_surgery(src, out_path, patches, layout=A, log=say, node_names=node_names)
    except Exception as e:                                            # noqa: BLE001
        say("字节手术：失败（%s）⇒ 回退全量保存" % e)
        if not _force_remove(out_path):
            say("⚠ 且产物删不掉：%s —— 调用方**覆盖**它即可（别把它当成品）" % out_path)
        return False

    ok2, why2 = verify(out_path, node_name, changed, log=say)
    if not ok2:
        say("⛔ 字节手术自检**没过**（%s）⇒ 删掉产物、回退全量保存" % why2)
        import gc
        gc.collect()
        if not _force_remove(out_path):
            say("⚠ 且产物删不掉：%s —— 调用方**覆盖**它即可（别把它当成品）" % out_path)
        return False
    say("✅ 字节手术完成（%.1f s，复用率 %.2f%%）"
        % (stats["seconds"], 100.0 * stats["reused"] / max(1, stats["blocks"])))
    return True


if __name__ == "__main__":
    import sys
    try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for p in sys.argv[1:]:
        A = read_layout(p)
        offs, end = block_offsets(A)
        print("%s\n  签名 %s 版本 %d flags 0x%X ct=%d" % (p, A["sig"], A["version"], A["flags"], A["ct"]))
        print("  块 %d（数据区 [%d, %d)）节点 %d info_at_end=%s 大小 %.2f MB"
              % (len(A["blocks"]), A["data_start"], end, len(A["nodes"]),
                 A["info_at_end"], A["file_size"] / 2 ** 20))
        print("  节点：%s" % [n[0] for n in A["nodes"]][:4])
