# -*- coding: utf-8 -*-
r"""旧版本发布件处置：**保留最近 N 版**（默认 3），更旧的**整体移进 `_archive\`**（⛔ 不再删）。

★ 政策变更（2026-09-16 21:12 用户拍板）：
  · **旧**：非当前版本的目录 / zip **直接 `Remove-Item`** ⇒ **不可逆**（v1.11.1 的安装包与源码包
    就是这样没的 ⇒ 事后想"回滚到上一版"**做不到**）。
  · **新**：**移动**到 `_archive\旧版本发布件\`（工作区根那份副本进 `...\根副本\`），
    按 **N=3** 只清理更旧的 ⇒ **仍可回滚**。实测成本 **≈581 MB/版**
    （发布目录 195.7 + 主 zip 114.7 + 源码 zip 78.0 + 工作区根副本 192.7）。
  · 更旧的一律**留痕**（本脚本打印《移档台账》；文件名保持原名 ⇒ **带版本号**）。
  · ⛔ `_archive\` 里**不与现有内容混名** ⇒ 固定进 `_archive\旧版本发布件\` 子目录。

为什么不能只靠人记得：每出一版都会留下 `BA_Mod_Maker_vX.Y.Z/` 目录和同名 zip，
而且**旧版 exe 摆在旁边极易被误双击运行**（用户真踩过"跑的是旧 exe ⇒ 修复没生效"）
⇒ 接进打包流程自动处置。**失败不影响本次打包结果**（`auto_build.py` 用 `check=False` 调它）。

保留：最近 N 版的目录 + zip（含当前版本）、`BA_Mod_Maker_blender_addon.zip`（插件包，版本无关）、
      `_build/`（PyInstaller 缓存）、源码与资源。
移档：其余 `BA_Mod_Maker_vX.Y.Z` 目录 / `BA_Mod_Maker_vX.Y.Z.zip` / `..._source.zip`
      （产品目录内与工作区根两处都算）。

用法：
    python clean_old_releases.py                  # 处置（移动，不删）
    python clean_old_releases.py --dry-run        # 只看会移什么（⛔ 不动盘）
    python clean_old_releases.py --keep 5         # 改保留版数（默认 3）
    python clean_old_releases.py --json           # 额外打印机读结果（给台账/清单用）

退出码：0 = 正常；2 = 有条目**处置失败或目标已存在**（⚠ 逐条打印，⛔ 绝不覆盖）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
#: 发布件可能出现的位置：工具目录 + 工作区根（package_zip 会往根目录放一份）
SEARCH_DIRS = [HERE, os.path.dirname(os.path.dirname(HERE))]
# 匹配 `BA_Mod_Maker_vX.Y.Z` 以及带后缀的形态（`..._source.zip` 等），
# 版本号单独捕获 —— 判定"是否保留"只看版本号，不看后缀。
NAME_RE = re.compile(r"^BA_Mod_Maker_v(\d+\.\d+\.\d+)(?:_.+)?$", re.IGNORECASE)
#: ★ 移档落点（⛔ 固定子目录 ⇒ 不与 `_archive\` 里既有内容混名；`_archive\` 现约 1.29 GB）
ARCHIVE_DIR = os.path.join(HERE, "_archive", "旧版本发布件")
#: 工作区根那份副本单独放一层 ⇒ 不与产品目录内的件同名互撞，且**出处可查**
ROOT_COPY_DIRNAME = "根副本"
DEFAULT_KEEP = 3


def current_version():
    r"""从 version.py 读当前版本（单一来源）。"""
    path = os.path.join(HERE, "version.py")
    with open(path, encoding="utf-8") as fh:
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', fh.read())
    if not m:
        raise SystemExit("version.py 里找不到 APP_VERSION")
    return m.group(1)


def _ver_tuple(v):
    return tuple(int(x) for x in v.split("."))


def _archive_dest(base, name):
    r"""`(来源基目录, 条目名)` → 归档落点（产品目录内的平放；工作区根的进 `根副本\`）。"""
    if os.path.abspath(base) == os.path.abspath(HERE):
        return os.path.join(ARCHIVE_DIR, name)
    return os.path.join(ARCHIVE_DIR, ROOT_COPY_DIRNAME, name)


def _scan():
    r"""→ `[(基目录, 条目名, 版本, 全路径), …]`（只扫两个基目录的**这一层**，⛔ 绝不递归）。"""
    found = []
    for base in SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for name in entries:
            stem = name[:-4] if name.lower().endswith(".zip") else name
            m = NAME_RE.match(stem)
            if m:
                found.append((base, name, m.group(1), os.path.join(base, name)))
    return found


def plan(keep=DEFAULT_KEEP, cur=None):
    r"""算出 `(cur, keep_set, kept, doomed)` —— **只读，不碰盘**（`--dry-run` 走的就是这条）。

    `keep_set` = 出现过的版本号里**最近 N 个** ∪ `{当前版本}`（当前版本可能还没出件 ⇒ 补进去无害）。
    """
    cur = cur or current_version()
    found = _scan()
    versions = sorted({v for _b, _n, v, _p in found} | {cur},
                      key=_ver_tuple, reverse=True)
    keep_set = set(versions[:max(1, int(keep))]) | {cur}
    kept, doomed = [], []
    for base, name, ver, full in found:
        if ver in keep_set:
            kept.append({"path": full, "version": ver, "name": name,
                         "why": "当前版本" if ver == cur else "最近 %d 版内" % keep})
        else:
            doomed.append({"path": full, "version": ver, "name": name,
                           "dest": _archive_dest(base, name)})
    return cur, keep_set, kept, doomed


def apply_move(doomed, dry_run=False):
    r"""逐条移档。⇒ `(moved, problems)`；**⛔ 目标已存在一律跳过、绝不覆盖**（覆盖 = 无声销毁）。"""
    moved, problems = [], []
    for item in doomed:
        src, dst = item["path"], item["dest"]
        if dry_run:
            moved.append(dict(item, status="would-move"))
            continue
        if os.path.exists(dst):
            problems.append(dict(item, status="dest-exists",
                                 detail="归档目标已存在 ⇒ 跳过（⛔ 不覆盖）"))
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)                 # ★ 移动，⛔ 不是 rmtree/remove
            moved.append(dict(item, status="moved"))
        except OSError as e:
            problems.append(dict(item, status="failed", detail=str(e)))
    return moved, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="旧版本发布件：保留最近 N 版 + 更旧的移进 _archive\\")
    ap.add_argument("--dry-run", action="store_true", help="只看会移什么（⛔ 不动盘）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留最近几版（默认 %d）" % DEFAULT_KEEP)
    ap.add_argument("--json", action="store_true", help="额外打印机读结果")
    ap.add_argument("--archive", default=None, help="覆盖归档落点（自测用）")
    args = ap.parse_args(argv)

    global ARCHIVE_DIR
    if args.archive:
        ARCHIVE_DIR = os.path.abspath(args.archive)

    cur, keep_set, kept, doomed = plan(keep=args.keep)
    moved, problems = apply_move(doomed, dry_run=args.dry_run)

    print("当前版本: v%s ｜ 保留策略: 最近 %d 版（含当前）｜ 归档落点: %s"
          % (cur, args.keep, ARCHIVE_DIR))
    for k in kept:
        print("  保留（%s）  %s" % (k["why"], k["path"]))
    if not doomed:
        print("  没有需要移档的旧版本 ✓")
    else:
        print("  %s %d 项旧版本%s："
              % ("将移档" if args.dry_run else "已移档", len(moved), "" if args.dry_run else "（**未删除**）"))
        for m in moved:
            print("    - %s\n        → %s" % (m["path"], m["dest"]))
    if problems:
        print("  ⚠ %d 项**未处置**（⛔ 不覆盖、不删）：" % len(problems))
        for p in problems:
            print("    - [%s] %s%s" % (p["status"], p["path"],
                                       ("  —— " + p["detail"]) if p.get("detail") else ""))
    if moved and not args.dry_run:
        # ★ 留痕用（照抄进 `技术资料\_branches\_handoff\_磁盘清理台账.md` / 《发布件清单》）
        print("  ── 移档台账（照抄留痕；本项目**删除=移档**，⛔ 无不可逆删除）")
        print("     处置者: clean_old_releases.py ｜ 保留 %d 版 ｜ 移档 %d 项 ｜ 落点 %s"
              % (args.keep, len(moved), ARCHIVE_DIR))
    if args.json:
        print(json.dumps({"version": cur, "keep": args.keep, "archive_dir": ARCHIVE_DIR,
                          "dry_run": args.dry_run, "kept": kept,
                          "moved": moved, "problems": problems},
                         ensure_ascii=False, indent=2))
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
