# -*- coding: utf-8 -*-
r"""清理旧版本发布件：只保留**当前版本**的 exe 目录 + zip。

⛔ 为什么需要：每出一版都会留下 `BA_Mod_Maker_vX.Y.Z/` 目录和同名 zip，各 ~115MB，
工作区很快堆满几十 GB；而且旧版 exe 摆在旁边很容易被误双击运行
（用户就踩过"跑的是旧 exe ⇒ 修复没生效"这类坑）。所以**接入打包流程自动清理**，
不靠人记得。

保留：当前版本的目录 + zip、`BA_Mod_Maker_blender_addon.zip`（插件包，版本无关）、
      `_build/`（PyInstaller 缓存）、源码与资源。
删除：其它 `BA_Mod_Maker_vX.Y.Z` 目录 / `BA_Mod_Maker_vX.Y.Z.zip`，
      以及发布目录里的 `_source.zip`（属于上一版源码包，会重新生成）。

用法：
    python clean_old_releases.py             # 清理
    python clean_old_releases.py --dry-run   # 只看会删什么
"""
from __future__ import annotations

import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
#: 发布件可能出现的位置：工具目录 + 工作区根（package_zip 会往根目录放一份）
SEARCH_DIRS = [HERE, os.path.dirname(os.path.dirname(HERE))]
# 匹配 `BA_Mod_Maker_vX.Y.Z` 以及带后缀的形态（`..._source.zip` 等），
# 版本号单独捕获 —— 判定"是否当前版本"只看版本号，不看后缀。
NAME_RE = re.compile(r"^BA_Mod_Maker_v(\d+\.\d+\.\d+)(?:_.+)?$", re.IGNORECASE)


def current_version():
    """从 version.py 读当前版本（单一来源）。"""
    path = os.path.join(HERE, "version.py")
    with open(path, encoding="utf-8") as fh:
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', fh.read())
    if not m:
        raise SystemExit("version.py 里找不到 APP_VERSION")
    return m.group(1)


def _ver_tuple(v):
    return tuple(int(x) for x in v.split("."))


def clean(dry_run=False):
    cur = current_version()
    removed, kept = [], []
    for base in SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        # 只扫这一层的 "BA_Mod_Maker*" 条目，绝不递归删除
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for name in entries:
            full = os.path.join(base, name)
            stem = name[:-4] if name.lower().endswith(".zip") else name
            m = NAME_RE.match(stem)
            if not m:
                continue
            # 版本号相同就保留（含 _source.zip 等派生件），否则删除
            if m.group(1) == cur:
                kept.append(full)
                continue
            try:
                if dry_run:
                    removed.append(full)
                    continue
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                removed.append(full)
            except OSError as e:
                print("  ! 删除失败 %s: %s" % (full, e))
    return cur, kept, removed


def main(argv):
    dry = "--dry-run" in argv
    cur, kept, removed = clean(dry)
    print("当前版本: v%s" % cur)
    for p in kept:
        print("  保留 %s" % p)
    if not removed:
        print("  没有需要清理的旧版本 ✓")
    else:
        print("  %s %d 项旧版本：" % ("将删除" if dry else "已删除", len(removed)))
        for p in removed:
            print("    - %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
