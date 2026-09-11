# -*- coding: utf-8 -*-
r"""崩溃诊断工具（断箭逆向分析工具箱）。

两个数据源：
1. Windows minidump（Sentry 在 %LocalLow%\SteelBalalaikaStudio\BrokenArrow\.sentry-native\reports\*.dmp）
   -> 解析异常码、异常地址、出错模块。
2. 游戏日志 GameLogs\Gamelog__*.log（游戏目录下）
   -> 搜索关键错误模式。

已知崩溃模式速查：
    "The referenced script (Unknown) ... is missing!" +
    "A scripted object ... different serialization layout ... (Read 32 bytes but expected 64 bytes)"
        = 战场加载时撞上陈旧对象（典型原因：构建累积旧对象/旧 preload 条目，
          见 REVERSE_ENGINEERING.md「已知坑」第 1 条）。军械库不受影响、战场崩溃。
    WER 事件: UnityPlayer.dll + 0x80000003 = Unity 内部断点（序列化布局错误等）。
    Sentry 有 dmp 而 WER 无 APPCRASH = 原生异常被 crashpad 捕获。

用法：
    python analyze_crash.py <xxx.dmp>          # 解析 minidump
    python analyze_crash.py --gamelogs <目录>  # 扫日志中的崩溃模式
    python analyze_crash.py --wer              # 最近 2 小时的 WER 崩溃事件
"""
import sys, os, struct, re, glob

# minidump 流类型
STREAM_MODULE_LIST = 4
STREAM_EXCEPTION = 6

EXC_CODES = {
    0xC0000005: "ACCESS_VIOLATION（访问违例，info[1]=被访问地址，0=空指针）",
    0xC0000094: "INT_DIVIDE_BY_ZERO",
    0xC00000FD: "STACK_OVERFLOW",
    0x80000003: "BREAKPOINT（Unity 内部断言/序列化布局错误）",
    0xC0000409: "FAST_FAIL",
    0xE0434352: "CLR_EXCEPTION（托管异常）",
    0xC000001D: "ILLEGAL_INSTRUCTION",
}


def read_md_string(data, rva):
    ln = struct.unpack_from("<I", data, rva)[0]
    return data[rva + 4:rva + 4 + ln].decode("utf-16-le", "replace").rstrip("\x00")


def parse_minidump(path):
    data = open(path, "rb").read()
    sig, ver, nstreams, dir_rva = struct.unpack_from("<4sIII", data, 0)
    if sig != b"MDMP":
        raise ValueError("不是有效 minidump（缺 MDMP 头）")
    streams = {}
    off = dir_rva
    for _ in range(nstreams):
        stype, dsize, rva = struct.unpack_from("<III", data, off)
        off += 12
        streams.setdefault(stype, []).append((dsize, rva))
    result = {"modules": [], "exception": None}
    if STREAM_MODULE_LIST in streams:
        dsize, rva = streams[STREAM_MODULE_LIST][0]
        n = struct.unpack_from("<I", data, rva)[0]
        off = rva + 4
        for _ in range(n):
            base, size, checksum, ts, namerva = struct.unpack_from("<QIIII", data, off)
            off += 108
            result["modules"].append({"name": read_md_string(data, namerva),
                                      "base": base, "size": size})
    if STREAM_EXCEPTION in streams:
        dsize, rva = streams[STREAM_EXCEPTION][0]
        tid, _ = struct.unpack_from("<II", data, rva)
        code, flags, rec, addr, nparams, _ = struct.unpack_from("<IIQQII", data, rva + 8)
        info = struct.unpack_from("<15Q", data, rva + 40)[0]
        result["exception"] = {"thread": tid, "code": code, "address": addr,
                               "nparams": nparams, "info": info}
    return result


def find_module(addr, mods):
    for m in mods:
        if m["base"] <= addr < m["base"] + m["size"]:
            return m, addr - m["base"]
    return None, 0


def print_minidump(path):
    r = parse_minidump(path)
    print(f"=== minidump: {path} ===")
    e = r["exception"]
    if e:
        name = EXC_CODES.get(e["code"], hex(e["code"]))
        print(f"异常码: {e['code']:#x} = {name}")
        mod, off = find_module(e["address"], r["modules"])
        if mod:
            print(f"异常地址: {e['address']:#x} = {mod['name']}+0x{off:x}")
        else:
            print(f"异常地址: {e['address']:#x}（不在任何已加载模块内）")
        if e["code"] == 0xC0000005 and e["nparams"] >= 2:
            print(f"访问地址: {e['info'][1]:#x}（{'写' if e['info'][0] else '读'}）")
    else:
        print("无异常流（可能是被捕获/正常退出的 dump）")
    print(f"模块数: {len(r['modules'])}")
    for m in r["modules"][:12]:
        print(f"  {m['base']:#x} +{m['size']:#x} {m['name']}")


LOG_PATTERNS = [
    ("different serialization layout", "序列化布局不匹配（陈旧对象/陈旧场景引用）"),
    ("script .* missing", "脚本丢失（MonoScript 解析失败）"),
    ("Read 32 bytes but expected", "读了 32B 但期望更多 → 对象数据被截断（累积构建/陈旧引用）"),
    ("missing.*turret_0", "turret_0 组件丢失"),
    ("Exception", "托管异常"),
    ("FMOD", "音频警告（一般无害）"),
]


def print_gamelogs(directory):
    pats = glob.glob(os.path.join(directory, "Gamelog__*.log"))
    pats.sort()
    print(f"=== GameLogs 扫描（{len(pats)} 个文件）===")
    for p in pats[-6:]:
        hits = set()
        text = open(p, encoding="utf-8", errors="replace").read()
        for pat, note in LOG_PATTERNS:
            if re.search(pat, text):
                hits.add((pat, note))
        print(f"{os.path.basename(p)}: {'正常' if not hits else ''}")
        for pat, note in sorted(hits):
            m = re.search(pat, text)
            print(f"   [{note}] 例: ...{text[max(0, m.start()-60):m.end()+80].strip()}...")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--gamelogs":
        print_gamelogs(argv[1] if len(argv) > 1 else
                        r"<游戏安装目录>\GameLogs")
        return 0
    if argv[0] == "--wer":
        import subprocess
        ps = ("Get-WinEvent -FilterHashtable @{LogName='Application'; "
              "StartTime=(Get-Date).AddHours(-2)} | Where-Object { $_.ProviderName -match "
              "'Windows Error Reporting|Application Error' } | Select-Object -First 8 "
              "TimeCreated, Id | Format-Table -AutoSize")
        subprocess.run(["powershell", "-Command", ps])
        return 0
    print_minidump(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
