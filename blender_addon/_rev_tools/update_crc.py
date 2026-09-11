# -*- coding: utf-8 -*-
"""自动更新 catalog.json 里各 bundle 的 CRC。

用法：
    python update_crc.py                # 弹窗选择 catalog.json
    python update_crc.py <catalog.json>              # 直接指定
    python update_crc.py <catalog.json> <crc.txt>    # 指定 CRC 来源文件

CRC 来源（按优先级自动找）：
    1. 命令行第二个参数指定的 crc.txt
    2. catalog.json 同级的 PC/ 目录下的 crc_list.txt（由 Unity 脚本生成）
    3. 游戏最新日志里的 "CRC Mismatch. Provided X, calculated Y"

crc.txt / crc_list.txt 格式：每行 "bundle文件名 = 十进制CRC"
（Unity 脚本 ComputeBundleCrc.cs 的「算整个文件夹」会直接生成这个文件）
"""
import sys, os, re, glob, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from catalog_mod import Catalog, read_object, write_object


def parse_crc_list(path):
    """读取 '文件名 = crc' 列表，返回 {hash: crc}（用文件名里的 32 位 hash 做键）。"""
    result = {}
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            name, crc = line.split("=", 1)
            name, crc = name.strip(), crc.strip()
            try:
                crc = int(crc)
            except ValueError:
                continue
            m = re.search(r"([0-9a-fA-F]{32})\.bundle", name)
            if m:
                result[m.group(1).lower()] = crc
            else:
                result[name.lower()] = crc
    return result


def parse_crc_from_log(log_path):
    """从游戏日志解析 CRC Mismatch，返回 {hash: crc}。"""
    text = open(log_path, encoding="utf-8", errors="replace").read()
    result = {}
    for provided, calculated, bundle in re.findall(
        r"CRC Mismatch\. Provided ([0-9a-fA-F]+), calculated ([0-9a-fA-F]+) from data\. Will not load AssetBundle '([^']+)'",
        text,
    ):
        m = re.search(r"([0-9a-fA-F]{32})\.bundle", bundle)
        key = m.group(1).lower() if m else bundle.lower()
        result[key] = int(calculated, 16)
    return result


def update_catalog(catalog_path, crc_map):
    """把 crc_map 里的新 CRC 写进 catalog 的 extraData（正确重建）。返回更新列表。"""
    cat = Catalog(catalog_path)
    entries = [list(e) for e in cat.entries]
    offsets = sorted({e[4] for e in entries if 0 <= e[4] < 0x80000000})

    objs = []
    updated = []
    for off in offsets:
        obj, _ = read_object(cat.extra, off)
        assert obj[0] == 7, f"extraData offset {off} 不是 JsonObject"
        asm, cls, jtext = obj[1]
        hm = re.search(r'"m_Hash":"([0-9a-fA-F]+)"', jtext)
        cm = re.search(r'"m_Crc":(\d+)', jtext)
        if hm and cm:
            h = hm.group(1).lower()
            if h in crc_map:
                new = crc_map[h]
                if new != int(cm.group(1)):
                    jtext = re.sub(r'"m_Crc":\d+', '"m_Crc":%d' % new, jtext, count=1)
                    updated.append((h, int(cm.group(1)), new))
        objs.append((7, (asm, cls, jtext)))

    if not updated:
        return []

    new_extra = bytearray()
    old_to_new = {}
    for off, obj in zip(offsets, objs):
        old_to_new[off] = len(new_extra)
        write_object(new_extra, obj[0], obj[1])
    for e in entries:
        if 0 <= e[4] < 0x80000000:
            e[4] = old_to_new[e[4]]

    cat.entries = [tuple(e) for e in entries]
    cat.extra = bytes(new_extra)
    cat.save(catalog_path)
    return updated


def pick_catalog():
    """无参数时弹窗选择 catalog.json。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw()
        path = filedialog.askopenfilename(
            title="选择 catalog.json",
            filetypes=[("catalog.json", "catalog.json"), ("所有文件", "*.*")],
        )
        root.destroy()
        return path
    except Exception:
        return None


def main():
    if len(sys.argv) >= 2:
        catalog_path = sys.argv[1]
    else:
        catalog_path = pick_catalog()
        if not catalog_path:
            print("未选择文件。用法: python update_crc.py <catalog.json>")
            return

    catalog_path = os.path.abspath(catalog_path)
    aa_dir = os.path.dirname(catalog_path)          # .../aa
    pc_dir = os.path.join(aa_dir, "PC")             # .../aa/PC
    # 游戏根目录 = aa 往上 3 层（aa -> StreamingAssets -> BrokenArrow_Data -> broken_arrow）
    game_dir = os.path.dirname(os.path.dirname(os.path.dirname(aa_dir)))

    crc_map = {}
    src_desc = ""
    if len(sys.argv) >= 3:
        crc_map = parse_crc_list(sys.argv[2])
        src_desc = sys.argv[2]
    else:
        lst = os.path.join(pc_dir, "crc_list.txt")
        if os.path.exists(lst):
            crc_map = parse_crc_list(lst)
            src_desc = lst
        else:
            logs = glob.glob(os.path.join(game_dir, "GameLogs", "*.log"))
            if logs:
                logs.sort(key=os.path.getmtime, reverse=True)
                crc_map = parse_crc_from_log(logs[0])
                src_desc = logs[0]

    if not crc_map:
        print("没找到 CRC 来源。")
        print("  方案一：在 Unity 里跑 ComputeBundleCrc.cs 的「算整个文件夹」，会自动生成 aa/PC/crc_list.txt，再运行本脚本")
        print("  方案二：先启动一次游戏触发 CRC 报错，再运行本脚本自动读日志")
        return

    print("CRC 来源:", src_desc, "(", len(crc_map), "个)")
    bak = catalog_path + ".bak_crc"
    if not os.path.exists(bak):
        shutil.copy(catalog_path, bak)
        print("已备份 ->", os.path.basename(bak))

    updated = update_catalog(catalog_path, crc_map)
    if updated:
        for h, old, new in updated:
            print(f"  {h}: {old} -> {new}")
        print(f"已更新 {len(updated)} 个 bundle 的 CRC，写入 {catalog_path}")
    else:
        print("没有匹配到需要更新的 bundle（CRC 已是最新，或文件名 hash 没对上）")


if __name__ == "__main__":
    main()
