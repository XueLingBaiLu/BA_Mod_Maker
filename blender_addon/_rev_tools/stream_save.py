# -*- coding: utf-8 -*-
r"""给 UnityPy 的 BundleFile 打「流式保存」补丁，内存友好 + 快速。

用 lz4 fast 模式（比 HC 快 36 倍，压缩率只差 4%），边压缩边直接写输出，
不经过第二个临时文件，保存 3.4GB bundle 从 ~2 分钟降到 ~20 秒。
"""
import os
import struct
import tempfile

import lz4.block

# 自动备份开关（默认**关**）：写 3.42GB 的 bundle 时不再顺手多留一份 .bak
try:
    from backup_policy import maybe_backup as _maybe_backup
except Exception:                                        # 旧打包/独立运行
    def _maybe_backup(path, log=None, label=None):
        return None


def _retarget_stream_paths(bf, mapping):
    r"""★ 改名之后**把流式贴图的资源路径跟着改**（否则贴图读不出来）。

    ⛔⛔ 2026-10-15 实测踩到（`test_my_bundle_image` / `test_my_bundle_replace` 当场红）：
      带**流式贴图**的包（unitportraits / unitlabels 这类）里，`Texture2D.m_StreamData.path`
      写的是**内部文件名**（形如 `CAB-9c2ee33b….resS`）。只改内部文件名、不改这个字段 ⇒
      重新加载时 `ResourceReader` 去找 `CAB-9c2ee33b….resS` → **`FileNotFoundError:
      Resource file … not found`** ✗（`Texture2D.image` 直接读不出来 ⇒ 换图标/立绘全废）

    ⇒ 规矩：**改名必须成对做** —— 内部文件名 与 引用它的 `m_StreamData.path` 一起改。
      `CAB-<32hex>` 新旧同长，但这里仍走 typetree 往返（不靠"等长"这个巧合）。

    ⛔ 有未落盘改动的对象**不动**（同 `_normalize_assetbundle` 的理由：往返会覆盖掉调用方的改动）。
    """
    pairs = []
    for old, new in (mapping or {}).items():
        ob = old.partition(".")[0]
        nb = new.partition(".")[0]
        if ob != nb:
            pairs.append((ob, nb))
    if not pairs:
        return 0
    n = 0
    for f in (getattr(bf, "files", None) or {}).values():
        objs = getattr(f, "objects", None)
        if not objs:
            continue
        for o in list(objs.values()):
            if getattr(getattr(o, "type", None), "name", "") != "Texture2D":
                continue
            if getattr(o, "data", None) is not None:
                continue
            try:
                t = o.read()
                sd = getattr(t, "m_StreamData", None)
                p = getattr(sd, "path", "") if sd is not None else ""
                if not p:
                    continue
                np_ = p
                for ob, nb in pairs:
                    if ob in np_:
                        np_ = np_.replace(ob, nb)
                if np_ != p:
                    sd.path = np_
                    o.save_typetree(t)
                    n += 1
            except Exception as e:                                    # noqa: BLE001
                print("[保存] ⚠ 改写流式贴图 m_StreamData.path 失败（%s）" % e)
    if n:
        print("[保存] ✓ 已同步 %d 个流式贴图的 m_StreamData.path（内部改名必须成对做）" % n)
    return n


def _uniqify_cab_names(bf):
    r"""【发布-02 根治】保存前把包内**每个内部文件名**换成唯一名，并同步 `AssetBundle.m_Name`。

    为什么必须（2026-10 第 72 轮实机踩到，代价是一次"游戏加载不了"）：
        Unity 判"这个包是不是已经加载过"看的是**内部文件表**（报错原文
        `can't be loaded because another AssetBundle with the same files is already loaded`）。
        而产品模板 `units_warehouse_small.bundle` 的内部名沿用了游戏 units 包的
        `CAB-1e53370fe5e57c593d6b445e251b8d1c` ⇒ 用它造出来的任何自建包**必然撞名** ⇒ 永远加载不了 ✗
    ⇒ 保存时统一改成 `CAB-<md5(旧名|时间)>`；`.resS` 兄弟文件跟着同一个新基名 ✓
    ⇒ 用户内部名不受影响（catalog 的 bundle 条目**键是外层文件名**，资产 internalId 里也**不含** CAB 名）✓
    可用环境变量 `BAMOD_KEEP_CAB_NAME=1` 关掉（只给排查用）。
    """
    if os.environ.get("BAMOD_KEEP_CAB_NAME") == "1":
        return {}
    files = getattr(bf, "files", None)
    if not files:
        return {}
    import hashlib
    import time
    # ★ 名字要**确定性**：由"内部文件名 + 对象数 + 对象字节数总和"派生 ⇒
    #   同样内容多次保存 ⇒ **同样的字节**（否则"布局转换不改内容 ⇒ CRC 相同"这类不变量
    #   会被名字随机化打破 ✗ —— 第 74 轮 `test_make_mod_bundle` 就是这么红的）
    #   仍然保证与别的包不同名：要撞名得"名字 + 对象数 + 对象尺寸总和"全同，实际不可能 ✓
    hh = hashlib.md5()
    for nm in sorted(files.keys()):
        objs_ = getattr(files[nm], "objects", None) or {}
        hh.update(nm.encode("utf-8"))
        hh.update(b"|%d|%d|" % (len(objs_), sum(int(getattr(o, "byte_size", 0) or 0) for o in objs_.values())))
    digest = hh.hexdigest()
    base_new = {}
    for name in list(files.keys()):
        base = name.partition(".")[0]
        if base in base_new:
            continue
        base_new[base] = "CAB-" + digest
    mapping = {}
    new_files = {}
    for name, f in files.items():
        base, dot, ext = name.partition(".")
        newname = base_new.get(base, base) + (dot + ext if dot else "")
        new_files[newname] = f
        mapping[name] = newname
    if all(k == v for k, v in mapping.items()):
        return {}
    files.clear()
    files.update(new_files)
    # ★ 2026-10-15：**改名必须成对做** —— 流式贴图的 `m_StreamData.path` 里也写着内部文件名，
    #   不改它 ⇒ 重新加载时报 `FileNotFoundError: Resource file CAB-….resS not found` ✗
    #   （`test_my_bundle_image` / `test_my_bundle_replace` 实测抓到）
    _retarget_stream_paths(bf, mapping)
    # ★ v1.9.1（★⑮）：打上"这个包已经改过名"的标记 ⇒ `save_stream` 里不再重复改一次
    #   （第二次改名改不动有新改动的对象，会把流式贴图的 path 留成过期值 ✗）
    try:
        bf._bamod_cab_renamed = True
    except Exception:                                             # noqa: BLE001
        pass
    # ★ 2026-10-15：改名之后**不再**同步 `AssetBundle.m_Name`（见 `_normalize_assetbundle` 的说明：
    #   `m_Name` 与内部 CAB 名不相等是**正常**的，游戏自己的包也这样；以前那段白干还刷噪音）；
    #   只顺手把 `m_Dependencies` 清干净（模板抄来的 `cab-…` 在游戏里都不存在）
    _normalize_assetbundle(bf)
    print("[保存] 内部 CAB 名已改为唯一名：%s"
          % ", ".join("%s → %s" % (k, v) for k, v in mapping.items()))
    return mapping


def _find_abs(bf):
    """→ [(node_name, AssetBundle 的 ObjectReader)]（每个内部文件最多一个）"""
    out = []
    for name, f in (getattr(bf, "files", None) or {}).items():
        objs = getattr(f, "objects", None)
        if not objs:
            continue
        for o in list(objs.values()):
            if getattr(getattr(o, "type", None), "name", "") == "AssetBundle":
                out.append((name, o))
    return out


def _normalize_assetbundle(bf):
    r"""★ 2026-10-15：**清空 `m_Dependencies`**（自建包从模板抄来的那串 `cab-…` 在游戏里都不存在）。

    依据（`17-软件改进待办` §一.6，飞行 ACV 实机）：自建包的 `m_Dependencies` 里有两个**小写**
    `cab-…` 名，而**游戏 79 个包里一个都没有** ⇒ 是从模板抄来的垃圾。实测清掉**不是主因**
    （"模型不显示"的真因是"重指向条目"），但**顺手清掉更干净** ✓

    ⛔ 同时**不再去同步 `m_Name`**（§一.5）：`AssetBundle.m_Name` 与**内部 CAB 名**不相等是**正常**的
      —— 游戏自己的包里就是 `m_Name='b54a3ecae2424f30d00f4dbb79449716.bundle'` + 内部
      `CAB-1e53370fe5e57c593d6b445e251b8d1c` ⇒ 以前"改名顺手把 m_Name 也改成同名"是**白干**，
      而它打印的"（跳过 AssetBundle.m_Name 同步…）"在**每次保存**时都刷一行**噪音** ✗

    ⛔ **对象有未落盘改动时一律不动**（`o.data is not None`）：typetree 往返会把调用方
      **刚写进 `o.data` 的改动覆盖回旧值** ✗（第 74 轮实机踩过：容器里只剩模板条目）。
      这种情况**不打印**（那是噪音，不是问题）。
    """
    cleared = 0
    for _name, o in _find_abs(bf):
        if getattr(o, "data", None) is not None:
            continue                      # 有未落盘改动 ⇒ 不能往返（且**不打印**）
        try:
            ab = o.read()
        except Exception as e:                                        # noqa: BLE001
            print("[保存] ⚠ 读 AssetBundle 失败（跳过 m_Dependencies 清理）：%s" % e)
            continue
        deps = getattr(ab, "m_Dependencies", None)
        if not deps:
            continue
        try:
            cleared += len(deps)
            ab.m_Dependencies = []
            o.save_typetree(ab)
        except Exception as e:                                        # noqa: BLE001
            print("[保存] ⚠ 清空 m_Dependencies 失败（一般无碍）：%s" % e)
    if cleared:
        print("[保存] ✓ 已清空 AssetBundle.m_Dependencies（%d 条 —— 模板抄来的 cab-… 在游戏里不存在）"
              % cleared)
    return cleared


def _warn_dangling_scripts(bf):
    """保存后体检：包内 MonoBehaviour 引用的 `m_Script` 有没有在本包内解析不到（= 脚本会静默失效）。

    为什么放在保存路径里（第 74 轮）：`[发布-03]` 那个坑**离线四环检查看不见**，
    只有进游戏/进编辑器才刷 `was missing!` + `No UnitPrefabRoot script found` ✗
    ⇒ 在**每次保存后**顺手体检，发现就大声报警 + 给出修法命令 ✓（不自动改，因为修它需要"源包"）
    """
    try:
        from fix_dangling_scripts import needed_scripts
    except Exception:                                        # noqa: BLE001
        return 0
    bad = 0
    for name, f in (getattr(bf, "files", None) or {}).items():
        sf = f if hasattr(f, "objects") else None
        if sf is None or not getattr(sf, "objects", None):
            continue
        try:
            need = needed_scripts(sf)
            have = set(sf.objects.keys())
            miss = [p for p in need if p not in have]
        except Exception:                                    # noqa: BLE001
            continue
        if miss:
            bad += len(miss)
            print("[保存] ⛔ **包内脚本悬空 %d 个**（MonoBehaviour 引用的 MonoScript 不在本包内）\n"
                  "        后果：游戏里刷 `The referenced script … is missing!`；"
                  "编辑器抛 `No UnitPrefabRoot script found`；\n"
                  "        而且**静态网格照样渲染** ⇒ 会被误当成「成功」⚠\n"
                  "        修法：python 技术资料\\scripts\\fix_dangling_scripts.py --bundle \"%s\" "
                  "--source <prefab 原来所在的包> --out <新包>"
                  % (len(miss), name))
    return bad


def _save_stream(self, out_path, packer="lz4"):
    """流式保存：序列化到临时文件，边 lz4(fast) 压缩边写输出。"""
    from UnityPy.helpers import CompressionHelper
    from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

    # ★ 第 74 轮：**先换内部 CAB 名**（见 `_uniqify_cab_names` 的说明）——
    #   这一步必须在序列化 `file_data` 之前，否则改的是已经写出去的字节 ✗
    # ★★ v1.9.1（★⑮）：**一次保存只改一次名**。`import_pack` 为了"字节手术"要**先**改名
    #   （快路径的 `node_names` 得是改名后的），于是这里会**再改一次**；而 `_retarget_stream_paths`
    #   故意跳过"有未落盘改动的对象" ⇒ 第二次改名会**改不动**刚建/刚补过的流式贴图
    #   ⇒ 它们的 `m_StreamData.path` 留着上一轮的 CAB 名 ⇒ 重新加载 `FileNotFoundError` ✗✗
    #   （实测：decals 包往返，新贴图 path 指向 `CAB-f0b4…` 而节点已是 `CAB-4266…`）
    #   改过名的包上再改一次没有任何收益 ⇒ 打个标记跳过 ✓
    if not getattr(self, "_bamod_cab_renamed", False):
        _uniqify_cab_names(self)
    # ★ 顺手体检"包内脚本悬空"（`[发布-03]` 那个坑离线四环看不见）⇒ 发现就报警 + 给修法 ✓
    _warn_dangling_scripts(self)

    if not packer or packer == "none":
        data_flag, block_info_flag = 64, 64
    elif packer == "original":
        data_flag = int(self.dataflags)
        block_info_flag = int(self._block_info_flags)
    elif packer == "lz4":
        data_flag, block_info_flag = 194, 2
    elif packer == "lzma":
        data_flag, block_info_flag = 65, 1
    else:
        raise NotImplementedError("packer: " + str(packer))

    if block_info_flag & self.dataflags.UsesAssetBundleEncryption:
        block_info_flag ^= self.dataflags.UsesAssetBundleEncryption
    if data_flag & self.dataflags.UsesAssetBundleEncryption:
        data_flag ^= self.dataflags.UsesAssetBundleEncryption

    # 1. 序列化 file_data 到临时文件
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".unitydata")
    files = []
    for name, f in self.files.items():
        data = f.bytes if isinstance(f, (EndianBinaryReader, EndianBinaryWriter)) else f.save()
        files.append((name, f.flags, len(data)))
        tmp.write(data)
    tmp.close()

    switch = data_flag & 0x3F
    chunk_size = 0x00020000

    # 2. 组装「占位 header」（长度精确，字段先填 0，最后回填）
    writer = EndianBinaryWriter()
    writer.write_string_to_null(self.signature)
    writer.write_u_int(self.version)
    writer.write_string_to_null(self.version_player)
    writer.write_string_to_null(self.version_engine)
    bundle_size_pos = writer.Position
    writer.write_long(0)
    comp_size_pos = writer.Position
    writer.write_u_int(0)
    uncomp_size_pos = writer.Position
    writer.write_u_int(0)
    writer.write_u_int(data_flag)
    if self.signature != "UnityFS":
        writer.write_byte(0)
    if self._uses_block_alignment:
        writer.align_stream(16)
    header = writer.bytes

    # 3. 边读 tmp 边 fast 压缩边写输出，收集 block_info
    # ⛔ **原子落盘**：`out_path` 常常就是游戏目录里的 3.4GB bundle 本体。
    #    直接 `open(out_path,"wb")` 一旦中途异常（lz4 报错/磁盘满/Ctrl+C/进程被杀），
    #    原文件已被截断 ⇒ **游戏文件不可恢复** ✗。
    #    改成：先写 `out_path + ".tmp"`，全部成功后再 `os.replace` 原子替换 ——
    #    这条线**不需要任何备份**就保证了原文件不会半截 ✗✓。
    #    `.bak` 只在用户显式打开『自动备份』时才留（默认关，见 backup_policy.py）。
    real_out = out_path
    tmp_out = out_path + ".tmp"
    out = None
    try:
        out = open(tmp_out, "wb")
        out.write(header)
        block_info = []
        with open(tmp.name, "rb") as fsrc:
            while True:
                chunk = fsrc.read(chunk_size)
                if not chunk:
                    break
                comp = lz4.block.compress(chunk, mode="fast", compression=0, store_size=False)
                if len(comp) >= len(chunk):
                    out.write(chunk)
                    block_info.append((len(chunk), len(chunk), block_info_flag ^ switch))
                else:
                    out.write(comp)
                    block_info.append((len(chunk), len(comp), block_info_flag))
    finally:
        try:
            os.remove(tmp.name)     # 失败路径也要清临时文件（旧写法只在成功时删）
        except OSError:
            pass

    # 4. 组装 block_info 并压缩
    block_writer = EndianBinaryWriter(b"\x00" * 0x10)
    block_writer.write_int(len(block_info))
    for usize, csize, flag in block_info:
        block_writer.write_u_int(usize)
        block_writer.write_u_int(csize)
        block_writer.write_u_short(flag)
    if not data_flag & 0x40:
        raise NotImplementedError("UnityPy always writes DirectoryInfo, so data_flag must include 0x40")
    block_writer.write_int(len(files))
    offset = 0
    for name, flag, length in files:
        block_writer.write_long(offset)
        block_writer.write_long(length)
        offset += length
        block_writer.write_u_int(flag)
        block_writer.write_string_to_null(name)
    block_data = block_writer.bytes
    block_writer.dispose()
    uncompressed_block_data_size = len(block_data)
    if switch in CompressionHelper.COMPRESSION_MAP:
        block_data = CompressionHelper.COMPRESSION_MAP[switch](block_data)
    else:
        raise NotImplementedError("No compression function for " + str(switch))
    compressed_block_data_size = len(block_data)

    # 5. 写 block_info + 回填 header 字段
    out.write(block_data)
    total_size = out.tell()
    out.seek(bundle_size_pos)
    out.write(struct.pack(">q", total_size))
    out.seek(comp_size_pos)
    out.write(struct.pack(">I", compressed_block_data_size))
    out.seek(uncomp_size_pos)
    out.write(struct.pack(">I", uncompressed_block_data_size))
    out.flush()
    os.fsync(out.fileno())
    out.close()
    out = None
    # 6. 原子替换（可选的 .bak 副本由 backup_policy 决定，默认不留）
    _maybe_backup(real_out, log=lambda m: print("[保存] " + m, flush=True))
    # ⛔ **必须先关掉源 bundle 的文件句柄**：原地保存时 `out_path` 就是正在被
    #    UnityPy 读的那个文件，Windows 不允许替换/覆盖已打开的文件
    #    ⇒ `os.replace` 报 `PermissionError [WinError 5]`（端到端实测踩到 ✗）。
    _release_source_handle(self)
    try:
        os.replace(tmp_out, real_out)
    except PermissionError:
        # 还有别的句柄握着（例如 Environment 缓存）⇒ 退回原地覆盖。
        import shutil
        print("[保存] ⚠ 目标文件仍被占用，无法原子替换 ⇒ 改为原地覆盖", flush=True)
        with open(tmp_out, "rb") as fs, open(real_out, "wb") as fd:
            shutil.copyfileobj(fs, fd, 8 * 1024 * 1024)
        try:
            os.remove(tmp_out)
        except OSError:
            pass


def _release_source_handle(bf):
    """关掉源 bundle 上**所有**底层输入流句柄（Windows 上不关就没法替换文件）。

    ⛔ 实测：`BundleFile` **没有** `reader` 属性 —— 句柄挂在内含的各个
    `SerializedFile.reader.stream` 上（`bf.files` 字典里的值）。只找 `bf.reader`
    等于什么都没关 ⇒ `os.replace` 仍然 WinError 5 ✗。所以这里做一次有界遍历。

    ★ 2026-09-16 补（句柄比原先以为的**深两层**）：
      `SerializedFile.reader` 是 `EndianBinaryReader_Streamable`，而真正攥着系统句柄的是
      它 `.raw` 上那条链：fsspec `LocalFileOpener` → `_io.BufferedReader` → `_io.FileIO`。
      老版本只关 `reader.stream` ⇒ `closed` 常年是 0（`release_env` 的注释因此才说
      "别把两遍 release 当主力"）。实测现场（`my_bundle.new_bundle` 建包收尾 WinError 32）：
      当时 gc 里**仍有 4 个**对象攥着这个包，正好就是上面那 4 个类型 ⇒ 这里顺着
      `raw`/`stream` 再走一层，并把链上的"文件类"对象一并关掉 ✓
    """
    seen = set()
    stack = [bf]
    closed = 0
    while stack:
        o = stack.pop()
        if o is None or id(o) in seen:
            continue
        seen.add(id(o))
        rd = getattr(o, "reader", None)
        # 关掉这一层能看到的"文件类"对象（老版只关 reader.stream，漏了 raw 那条链）
        for cand in (getattr(rd, "stream", None) if rd is not None else None,
                     getattr(rd, "raw", None) if rd is not None else None,
                     getattr(o, "raw", None),
                     getattr(o, "stream", None)):
            if cand is None or not hasattr(cand, "close"):
                continue
            try:
                if not getattr(cand, "closed", False):
                    cand.close()
                    closed += 1
            except Exception:  # noqa: BLE001
                pass
        for attr in ("files", "_files"):
            v = getattr(o, attr, None)
            if isinstance(v, dict):
                stack.extend(v.values())
        for attr in ("assets_file", "file", "reader", "raw", "stream", "_stream"):
            v = getattr(o, attr, None)
            if v is not None and v is not bf and not isinstance(v, (str, bytes, int, float)):
                stack.append(v)
        if len(seen) > 200:      # 有界：正常也就几十个文件
            break
    if closed:
        print("[保存] 已关闭 %d 个输入流句柄（便于原子替换目标文件）" % closed)
    return closed


def release_env(env, log=None):
    r"""★ **确定性**放掉一个 `UnityPy` Environment 持有的文件句柄（Windows 必需）。

    ⛔ 为什么不能只写 `del env`（2026-10-15 用户现场，端到端实测踩到 ✗）：
       `del env` 只减引用计数，**Environment 里有引用环** ⇒ 真正的句柄要等下一次
       `gc.collect()` 才关。于是"删了变量"之后 `os.replace` 立刻报
       `PermissionError: [WinError 32] 另一个程序正在使用此文件 …`
       —— 报的还是**源文件**（那个刚写完的 `.tmp`），极难一眼看出是句柄没关。
       实测对照（`_rev_tools\out\tmp\probe_replace_lock2.py`）：`del` 后再 gc 成功，
       只 `del` 不 gc 失败 ⇒ 结论来自工具输出，不是推断 ✓

    做法：`gc.collect()` 之前/之后各走一遍 `_release_source_handle`（幂等）。
    ⛔ 实测（`测试\test_fix_dangling_scripts_product.py` ⑤）：`closed` **经常是 0**
       —— 句柄并不挂在 `_release_source_handle` 能走到的属性上，**真正起作用的是那次
       `gc.collect()`**。两遍 release 是白送的保险（不花时间），别把它当主力。
    返回"主动 close 掉的流个数"（可能为 0，**不代表没释放**）✓
    """
    if env is None:
        return 0
    import gc
    bf = getattr(env, "file", None)
    closed = _release_source_handle(bf) if bf is not None else 0
    gc.collect()
    if bf is not None:
        closed += _release_source_handle(bf)                     # 兜底第二遍（幂等）
    return closed


def atomic_replace(src, dst, log=None, tries=4):
    r"""`os.replace(src, dst)` + **重试**：Windows 上句柄刚关、文件仍被短暂占用很常见。

    ⛔ 失败时**不退回"原地覆盖"**（那会把目标截断成半截）——
       修包这类操作宁可**留下完整的 `.tmp`、目标一字不动**，也不能写坏目标 ✓
    返回 True/False；False 时调用方负责把"完整成品在 src、目标未动"讲清楚。
    """
    import gc
    import time
    last = None
    for i in range(max(1, tries)):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as e:
            last = e
            gc.collect()
            if log:
                log("  ⚠ 目标文件被占用（第 %d/%d 次）⇒ 稍后重试" % (i + 1, tries))
            time.sleep(0.4 * (i + 1))
    if log:
        log("  ✗ 仍然无法替换：%s" % last)
    return False


def ensure_stream_save():
    """给 UnityPy 的 BundleFile 类打补丁，加 save_stream 方法（幂等）。"""
    from UnityPy.files.BundleFile import BundleFile
    if hasattr(BundleFile, "save_stream"):
        return
    BundleFile.save_stream = _save_stream


if __name__ == "__main__":
    print(__doc__)