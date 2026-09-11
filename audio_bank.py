# -*- coding: utf-8 -*-
"""往 FMOD 音库注入新音效：FSB5(PCM16) 构建 + 加密 + SND 区拼接 + 事件/波形/字符串注册。

格式依据（见 .re-kb/data-structures/fmod-bank-encryption.md 与 fmod-studio-bank-format.md）：
- bank = RIFF/FEV → FMT → LIST PROJ（EVTS 事件段 / WAVS 波形表 / SNDH 样本索引 / STDT 字符串表）
  → SND chunk（样本数据）。
- 样本区 = 用密钥 jU5n9Ce2ng5T 按 FMOD 位反转+XOR 加密的标准 FSB5。
- 事件名 → GUID 的查找表在 Master.strings.bank（STDT 片段树 + 索引）。
"""
import io
import os
import struct
import wave
import hashlib
import uuid

FMOD_KEY = b"jU5n9Ce2ng5T"
_FOURBIT = [0b0000, 0b1000, 0b0100, 0b1100, 0b0010, 0b1010, 0b0110, 0b1110,
            0b0001, 0b1001, 0b0101, 0b1101, 0b0011, 0b1011, 0b0111, 0b1111]
_REV = [_FOURBIT[b >> 4] | (_FOURBIT[b % 16] << 4) for b in range(256)]
_FREQ_CODES = {8000: 1, 11000: 2, 11025: 3, 16000: 4, 22050: 5,
               24000: 6, 32000: 7, 44100: 8, 48000: 9}


def fmod_encrypt(data, key=FMOD_KEY):
    """FMOD 样本加密：out[i] = REV(in[i] ^ key[i%len])。"""
    out = bytearray(data)
    for i in range(len(out)):
        out[i] = _REV[out[i] ^ key[i % len(key)]]
    return bytes(out)


def fmod_decrypt(data, key=FMOD_KEY):
    """FMOD 样本解密：out[i] = REV(in[i]) ^ key[i%len]。"""
    out = bytearray(data)
    for i in range(len(out)):
        out[i] = _REV[out[i]] ^ key[i % len(key)]
    return bytes(out)


fmod_crypt = fmod_encrypt  # 兼容名（注意：加解密不同向！）


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def wav_to_pcm16(wav_bytes):
    """解析 PCM16 WAV → (sample_rate, channels, pcm_data)。非 PCM16 抛错。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
            raise ValueError("音频必须是 PCM 16-bit WAV")
        rate = w.getframerate()
        ch = w.getnchannels()
        data = w.readframes(w.getnframes())
        return rate, ch, data


def build_fsb5_pcm16(wav_bytes, sample_name="mod_sample"):
    """把 PCM16 WAV 构建成 FSB5 blob（单样本，mode=2 PCM16）。"""
    rate, ch, pcm = wav_to_pcm16(wav_bytes)
    freq_code = _FREQ_CODES.get(rate)
    if freq_code is None:
        raise ValueError("采样率 %d 不支持（支持：%s）" % (rate, sorted(_FREQ_CODES)))
    name = (sample_name.encode("utf-8") + b"\x00")
    name_table = struct.pack("<I", 0) + name  # 偏移 0 + 名字
    num_samples = len(pcm) // (2 * ch)
    # 样本头：u64 位域 {bit0 next=0, bits1-4 freq, bit5 双声道, bits6-33 dataOffset(×16),
    #                  bits34-63 样本数}；PCM16 无额外块，补齐到 80B。
    data_offset_units = 5  # 80B 样本头 → 数据从 16×5=80 起（头 60 + 80 + 名称表对齐到 16）
    hdr_size = 60 + 80 + len(name_table)
    data_offset_units = (hdr_size + 15) // 16
    raw = (1 << 34) | num_samples  # samples 在 bits 34-63
    raw |= (0 << 33)  # bit33 保留
    raw |= (data_offset_units << 6)
    raw |= (0 if ch == 1 else 1) << 5
    raw |= freq_code << 1
    sample_header = struct.pack("<Q", raw) + b"\x00" * 72
    data = pcm
    data_pad = b"\x00" * (data_offset_units * 16 - hdr_size)
    # 组装 FSB5
    fsb = bytearray()
    fsb += b"FSB5"
    fsb += struct.pack("<I", 1)            # version
    fsb += struct.pack("<I", 1)            # numSamples
    fsb += struct.pack("<I", len(sample_header))  # sampleHeadersSize
    fsb += struct.pack("<I", len(name_table))     # nameTableSize
    fsb += struct.pack("<I", len(data_pad) + len(data))  # dataSize
    fsb += struct.pack("<I", 2)            # mode = PCM16
    fsb += b"\x00" * 8                     # zero
    fsb += b"\x00" * 16                    # hash（先清零，最后回填 MD5）
    fsb += b"\x00" * 8                     # dump
    fsb += sample_header
    fsb += name_table
    fsb += data_pad + data
    # hash = MD5(整段 hash 清零)
    digest = hashlib.md5(bytes(fsb)).digest()
    fsb[36:52] = digest
    return bytes(fsb)


def parse_chunks(data, start=12):
    """解析 RIFF chunk 树 → [{tag, off, size, listtype}]（顶层）。"""
    out = []
    off = start
    while off + 8 <= len(data):
        tag = data[off:off + 4]
        size = u32(data, off + 4)
        lt = data[off + 8:off + 12].decode("latin1") if tag == b"LIST" else ""
        out.append({"tag": tag.decode("latin1"), "off": off, "size": size, "listtype": lt})
        off += 8 + size + (size & 1)
    return out


def _fix_ripp_size(data):
    struct.pack_into("<I", data, 4, len(data) - 8)


def _backup_once(path):
    bak = path + ".bak"
    if not os.path.exists(bak):
        import shutil
        shutil.copy2(path, bak)
    return bak


def add_sound(sa, event_name, wav_bytes, bank_name="CustomDialog.bank", progress=None):
    """完整添加音效：构建 FSB5(PCM16)→加密→重建音库（事件 EVTS + 波形 WAVS +
    样本 SND/SNDH + 总线复制 + HASH 表）。自动备份。

    注意：Master.strings.bank 的路径→GUID 字符串表注册尚未实现（STDT 索引格式
    未完全解出）——游戏按名解析新事件前需要它；本函数会提示这一点。
    返回 (event_guid_hex, 样本数)。
    """
    def step(msg):
        if progress:
            progress(msg)
    live_path = os.path.join(sa, bank_name)
    if not os.path.isfile(live_path):
        raise FileNotFoundError("音库不存在：%s" % bank_name)
    # 备份原件（.bak = 原始音库；重复添加时以原件为重建基底）
    _backup_once(live_path)
    bank_path = live_path + ".bak"
    _chk = open(bank_path, "rb").read()
    _s = _chk.find(b"SNDH")
    if _s > 0 and u32(_chk, _s + 4) > 0:
        raise ValueError("目标音库已有样本（%s），重建会覆盖它——请改用 "
                         "CustomDialog.bank 这类空音库" % bank_name)
    strings_path = os.path.join(sa, "Master.strings.bank")
    if not os.path.isfile(strings_path):
        raise FileNotFoundError("Master.strings.bank 不存在")
    _backup_once(strings_path)

    # 1) FSB5 + 加密
    step("构建 PCM16 样本 ...")
    fsb = build_fsb5_pcm16(wav_bytes, os.path.basename(event_name))
    enc = fmod_encrypt(fsb)
    # 2) 重建音库：事件 + 波形 + 样本 + 总线 + HASH（以 .bak 原件为基底，写入 live 路径）
    step("重建 %s（事件 + 波形 + 样本）..." % bank_name)
    new_bytes, event_guid, wav_guid = rebuild_bank_with_event(bank_path, event_name, enc)
    open(live_path, "wb").write(new_bytes)
    step("完成：事件 %s GUID=%s WAV=%s" % (event_name, event_guid, wav_guid))
    step("提示：Master.strings.bank 路径注册为实验性（格式未完全解出）；"
         "进游戏实测，若音效不响请看 GameLogs 的 FMOD 报错")
    return event_guid, 1


def _find_template_bank():
    """模板 bank（含单事件 + 总线结构）：SFX_Weapon_BB_DLC.bank。"""
    import glob
    roots = [
        "D:/Steam/steamapps/common",
        "C:/Program Files (x86)/Steam/steamapps/common",
        "C:/Program Files/Steam/steamapps/common",
        "E:/Steam/steamapps/common",
        "F:/Steam/steamapps/common",
    ]
    for root in roots:
        for g in glob.glob(os.path.join(root, "*")):
            base = os.path.basename(g).lower()
            if not (base.startswith("broken") or base.startswith("arrow")):
                continue
            p = os.path.join(g, "BrokenArrow_Data", "StreamingAssets", "SFX_Weapon_BB_DLC.bank")
            if os.path.isfile(p):
                return p
    raise FileNotFoundError("找不到模板音库 SFX_Weapon_BB_DLC.bank（游戏目录未检测到）")


SECTION_ORDER = ["IBSS", "GBSS", "RBSS", "MBSS", "BEFX", "PEFX", "SEFX", "SCFX",
                 "VCAS", "EVTS", "TLNS", "PMLS", "PRMS", "CTRS", "CRVS", "MPGS",
                 "MUIS", "SPIS", "PRIS", "EVIS", "WAIS", "EFIS", "CMDS", "SLNS",
                 "LWVS", "WAVS", "SNAS", "MODS"]


def _fnv1a32(data):
    h = 0x811C9DC5
    for c in data:
        h ^= c
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _p16(v):
    return struct.pack("<H", v)


def _p32(v):
    return struct.pack("<I", v)


def _list_chunk(typ, content):
    return b"LIST" + _p32(4 + len(content)) + typ + content


def rebuild_bank_with_event(bank_path, event_name, encrypted_fsb5):
    """在 bank 里注入 1 个新事件（照抄模板单事件结构换 GUID）+ 1 条 WAV +
    复制模板总线段（让事件有输出路由）+ 追加加密 FSB5 样本 + HASH 表条目。

    不写文件，返回 (新 bank 字节, event_guid_hex, wav_guid_hex)。
    strings bank 注册由调用方处理（当前未实现）。
    """
    import uuid
    tmpl_path = _find_template_bank()
    orig = open(bank_path, "rb").read()
    tmpl = open(tmpl_path, "rb").read()
    assert orig[:4] == b"RIFF" and orig[8:12] == b"FEV ", "目标不是 FMOD bank"

    # ---- 模板素材 ----
    def _extract_first_evnt(buf):
        evts = buf.find(b"EVTS") - 8
        pos = evts + 12 + 12  # LIST头(8)+type(4)+LCNT块(12)
        assert buf[pos:pos + 4] == b"LIST" and buf[pos + 8:pos + 12] == b"EVNT"
        lsize = u32(buf, pos + 4)
        return bytes(buf[pos: pos + 8 + lsize + (lsize & 1)])

    def _extract_section(buf, name):
        i = buf.find(name) - 8
        assert i > 0, name
        return bytes(buf[i: i + 8 + u32(buf, i + 4) + (u32(buf, i + 4) & 1)])

    evnt_blob = _extract_first_evnt(tmpl)
    # 模板总线段（事件路由：IBUS/GBUS/RBUS/MBUS 全量复制，内部 GUID 自洽）
    bus_sections = {t: _extract_section(tmpl, t.encode())
                    for t in ("IBSS", "GBSS", "RBSS", "MBSS")}

    # ---- 新 GUID ----
    new_event_guid = uuid.uuid4().bytes
    new_wav_guid = uuid.uuid4().bytes

    # ---- EVTS：模板换 GUID（G2/G3/G4 保持模板值——它们引用复制过来的总线）----
    blob = bytearray(evnt_blob)
    evtb_off = 20  # LIST(8)+"EVNT"(4)+EVTB块头(8)
    blob[evtb_off: evtb_off + 16] = new_event_guid
    evts = _list_chunk(b"EVTS", b"LCNT" + _p32(4) + _p32(1) + bytes(blob))

    # ---- WAVS：新波形记录 {GUID, 12, FSB5样本索引0, flags, 0} ----
    wav = b"WAV " + _p32(30) + new_wav_guid + _p32(12) + _p32(0) + _p32(0x02000000) + _p16(0)
    wavs = _list_chunk(b"WAVS", b"LCNT" + _p32(4) + _p32(1) + wav)

    # ---- BNKI ----
    bnk_guid = orig[orig.find(b"BNKI") + 8: orig.find(b"BNKI") + 24]
    bnki = b"BNKI" + _p32(32) + bnk_guid + _p32(0) + _p32(0) + _p32(1) + _p32(7)

    # ---- 组装 PROJ ----
    sections = {t: _list_chunk(t.encode(), b"") for t in SECTION_ORDER}
    for t in ("IBSS", "GBSS", "RBSS", "MBSS"):
        sections[t] = bus_sections[t]  # 模板总线全量复制（GUID 自洽，事件路由可用）
    sections["EVTS"] = evts
    sections["WAVS"] = wavs

    # SND 区偏移预算
    def _chunk_len(b):
        return len(b)

    proj_fixed = b""
    proj_fixed += bnki
    for t in SECTION_ORDER:
        proj_fixed += sections[t]
    # 尾部块（SNDH 12B 数据 + STDT/STBL 空 + HASH + DEL/MUTE/REFI/PLAT 空）
    path_bytes = event_name.encode("utf-8")
    h_hash = _fnv1a32(path_bytes)
    # HASH：4B 头 + {GUID, u32 hash} × 2（事件/WAV；hash 为占位 fnv1a32(path)）
    hash_chunk = (b"HASH" + _p32(4 + 20 * 2) + _p32(0x0014009D)
                  + new_event_guid + _p32(h_hash)
                  + new_wav_guid + _p32(h_hash))
    tail = hash_chunk
    for tag in (b"STDT", b"STBL", b"DEL ", b"MUTE", b"REFI", b"PLAT"):
        tail += tag + _p32(0)
    sndh_len = 8 + 12
    # RIFF(12) + FMT(16) + LIST PROJ 头(12) + BNKI + 28 段 + SNDH + 尾部块 + SND 头(8)
    fixed_before_snd_data = (12 + 16 + 12 + len(bnki)
                             + sum(len(sections[t]) for t in SECTION_ORDER)
                             + sndh_len + len(tail))
    snd_data_start = fixed_before_snd_data + 8
    pad = (-snd_data_start) % 32
    fsb5_abs_off = snd_data_start + pad
    sndh = b"SNDH" + _p32(12) + _p16(1) + _p16(8) + _p32(fsb5_abs_off) + _p32(len(encrypted_fsb5))

    proj_content = bnki
    for t in SECTION_ORDER:
        proj_content += sections[t]
    proj_content += sndh
    proj_content += hash_chunk
    for tag in (b"STDT", b"STBL", b"DEL ", b"MUTE", b"REFI", b"PLAT"):
        proj_content += tag + _p32(0)

    proj_list = _list_chunk(b"PROJ", proj_content)
    fmt = orig[12:28]
    snd_data = b"\x00" * pad + encrypted_fsb5
    snd_chunk = b"SND " + _p32(len(snd_data)) + snd_data
    riff_size = 4 + len(fmt) + len(proj_list) + len(snd_chunk)
    out = b"RIFF" + _p32(riff_size) + b"FEV " + fmt + proj_list + snd_chunk
    return out, new_event_guid.hex().upper(), new_wav_guid.hex().upper()


def inject_fsb5(bank_path, out_path, fsb5, snd_pad=16):
    """把加密后的 FSB5 追加进 bank：顶层加 SND chunk、SNDH 加条目、修 RIFF/PROJ size。

    布局与真实 bank 一致：SND 为 PROJ 外部的顶层 chunk，SNDH 条目 offset 指向
    SND 数据内的 FSB5（16 字节零填充后）。返回 (新 SNDH 条目 offset, size)。
    """
    data = bytearray(open(bank_path, "rb").read())
    chunks = parse_chunks(data)
    proj = next(c for c in chunks if c["tag"] == "LIST" and c["listtype"] == "PROJ")
    sndh = data.find(b"SNDH")
    if sndh < 0:
        raise ValueError("bank 里没有 SNDH chunk")
    old_sndh_size = u32(data, sndh + 4)
    # 旧 SNDH 数据（size 0 时没有 cnt/ver，按 {0, 8} 处理）
    if old_sndh_size >= 4:
        cnt, ver = struct.unpack_from("<HH", data, sndh + 8)
    else:
        cnt, ver = 0, 8
    snd_pos = len(data)            # 新 SND chunk 位置（替换 SNDH 前的文件长度）
    # 新 SNDH chunk（先构造，算出增长量后再定 FSB5 偏移）
    new_sndh = struct.pack("<HHII", cnt + 1, ver, 0, len(fsb5))
    growth = (8 + len(new_sndh)) - (8 + old_sndh_size)
    fsb_offset = snd_pos + growth + 8 + snd_pad
    new_sndh = struct.pack("<HHII", cnt + 1, ver, fsb_offset, len(fsb5))
    data = (data[:sndh] + b"SNDH" + struct.pack("<I", len(new_sndh)) + new_sndh
            + data[sndh + 8 + old_sndh_size:])
    # 追加顶层 SND chunk
    data += b"SND " + struct.pack("<I", snd_pad + len(fsb5))
    data += b"\x00" * snd_pad + fsb5
    # PROJ LIST size 增长 = SNDH chunk 增长
    struct.pack_into("<I", data, proj["off"] + 4, proj["size"] + growth)
    _fix_ripp_size(data)
    open(out_path, "wb").write(bytes(data))
    return fsb_offset, len(fsb5)


if __name__ == "__main__":
    import math
    # 自测：1 秒 440Hz 正弦波 → FSB5 → 注入 CustomDialog 副本
    rate, ch = 44100, 1
    n = rate
    pcm = bytearray()
    for i in range(n):
        v = int(32767 * 0.5 * math.sin(2 * math.pi * 440 * i / rate))
        pcm += struct.pack("<h", v)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    fsb = build_fsb5_pcm16(wav.getvalue(), "TestTone")
    print("FSB5:", len(fsb), "bytes, magic:", fsb[:4])
    # 验证：加密再解密应还原
    enc = fmod_encrypt(fsb)
    dec = fmod_decrypt(enc)
    print("crypt roundtrip:", dec == fsb)
    src = r"<游戏安装目录>\BrokenArrow_Data\StreamingAssets\CustomDialog.bank"
    dst = r"<工作目录>\CustomDialog_test.bank"
    off, size = inject_fsb5(src, dst, enc)
    print("injected:", dst, "fsb at", hex(off), "size", size)
    # 验证注入结果可解析
    d2 = open(dst, "rb").read()
    chunks = parse_chunks(d2)
    print("chunks:", [(c["tag"], c["off"], c["size"]) for c in chunks])
    sndh = d2.find(b"SNDH")
    cnt, ver = struct.unpack_from("<HH", d2, sndh + 8)
    print("SNDH entries:", cnt, "ver:", ver, "first entry:", hex(u32(d2, sndh + 12)), u32(d2, sndh + 16))
    fsb_off = u32(d2, sndh + 12)
    dec2 = fmod_decrypt(d2[fsb_off:fsb_off + u32(d2, sndh + 16)])
    print("recovered magic:", dec2[:4], "samples:", u32(dec2, 8), "mode:", u32(dec2, 24))
    assert dec2[:4] == b"FSB5" and u32(dec2, 24) == 2
    print("INJECT SELF-TEST PASS")
