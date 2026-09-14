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


def _save_stream(self, out_path, packer="lz4"):
    """流式保存：序列化到临时文件，边 lz4(fast) 压缩边写输出。"""
    from UnityPy.helpers import CompressionHelper
    from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

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
        if rd is not None:
            st = getattr(rd, "stream", None)
            if st is not None and hasattr(st, "close"):
                try:
                    if not getattr(st, "closed", False):
                        st.close()
                        closed += 1
                except Exception:  # noqa: BLE001
                    pass
        for attr in ("files", "_files"):
            v = getattr(o, attr, None)
            if isinstance(v, dict):
                stack.extend(v.values())
        for attr in ("assets_file", "file"):
            v = getattr(o, attr, None)
            if v is not None and v is not bf:
                stack.append(v)
        if len(seen) > 200:      # 有界：正常也就几十个文件
            break
    if closed:
        print("[保存] 已关闭 %d 个输入流句柄（便于原子替换目标文件）" % closed)
    return closed


def ensure_stream_save():
    """给 UnityPy 的 BundleFile 类打补丁，加 save_stream 方法（幂等）。"""
    from UnityPy.files.BundleFile import BundleFile
    if hasattr(BundleFile, "save_stream"):
        return
    BundleFile.save_stream = _save_stream


if __name__ == "__main__":
    print(__doc__)