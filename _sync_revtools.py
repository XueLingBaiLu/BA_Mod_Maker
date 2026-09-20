# -*- coding: utf-8 -*-
r"""**双向显式同步 + 分叉检测**：`blender_addon/_rev_tools` ↔ 根目录 `_rev_tools`。

为什么需要它（v1.8.60）：
  两个 `_rev_tools` 目录曾经**各自演化** ⇒ 实测出现分叉：
  `hub_edit.py` 12554 vs 18896 字节、`copy_full.py` 14543 vs 28922 字节、
  `compute_bundle_crc.py` 少一个 `import re`（跑 `compute_dir` 必崩 ✗）。
  同名的运行期模块在两处不一致，就会复现「改了一处、另一处还是旧的」这类最难查的 bug。
  ⇒ 定规则：**同名运行期模块在两处必须逐字节一致**。
  根目录多出来的开发脚本（build_final / make_icons / _scan_* …）**不动**。

★★ 2026-09-17（★㉑ 现场 · 中枢裁定）：**默认方向 ⛔ 不再硬编码 `addon → 根`** ——
  实测 **exe 消费的是根目录那份**（`build_exe.py` 的 `--add-data … "_rev_tools", "my_bundle_catalog.py"`），
  而 ★㉑ 的修复正好落在**根侧** ⇒ 若仍按老默认（addon → 根）同步，会**把根侧的修复回退掉** ✗。
  ⇒ 现在**方向显式化**，并**必须打印「方向 ＋ 判据」**；不给方向时按 **mtime 自动判**（新的一侧作源），
  **两侧各有更新（混合分叉）时拒绝执行**并要求显式指定方向。

用法：
    python _sync_revtools.py                                            # ⛔ 不给方向 ⇒ 自动判（打印判据）
    python _sync_revtools.py --check                                    # 只检查（有分叉 ⇒ 返回码 2）
    python _sync_revtools.py --direction addon<-root                    # 根 → addon（★ 推荐：以根侧为准）
    python _sync_revtools.py --direction root<-addon                    # addon → 根（老默认方向；⛔ 可能回退根侧修复）
    python _sync_revtools.py --from root --to addon                     # 另一种写法（等价于 addon<-root）

安全闸（保留 · v1.8.60 教训）：**目标侧的副本比源侧更新 ⇒ 拒绝自动覆盖**（返回码 3）——
  本脚本历史上把「改在目标副本上的修复」直接吃掉过一次。
返回码：0 一致或已同步 ｜ 2 只检查且有分叉 ｜ 3 拒绝（目标更新／方向判不出／方向冲突）
"""
import filecmp
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "blender_addon", "_rev_tools")
ROOT = os.path.join(HERE, "_rev_tools")
SIDES = {"addon": ADDON, "root": ROOT}

# 运行期模块：两份必须逐字节一致（插件与 exe 都会用到）
SHARED = "*.py"
# 只在源头存在、不需要同步过去的（无）——保留开关，将来要排除某些文件时用
EXCLUDE_SYNC = set()

# ★ 2026-09-18：**已声明的单侧件**（只在根侧 `_rev_tools\` 存在，按设计**不参与**"两份一致"检查）——
#   ⛔ 不是"静默豁免"：`--check` 会把根侧独有清单**打出来**（已声明的带理由、未声明的标 ⚠）。
SINGLE_SIDE_ROOT = {
    "bundle_asset_lookup_core.py":
        "[界面-04] 「prefab ↔ 映射路径」双向检索的**唯一正本**：只被 my_bundle.py（GUI/exe 侧）与 "
        "技术资料\\scripts\\bundle_asset_lookup.py（CLI 薄壳）import；**Blender 插件侧不用它** ⇒ 无需镜像。"
        "⛔ 它仍必须随 exe 走（见 build_exe.py 的 --add-data 与 auto_build.py 的 WATCH_FILES 成对条目）。",
}


def _shared_files(src_dir):
    if not os.path.isdir(src_dir):
        raise SystemExit("找不到源目录：%s" % src_dir)
    return sorted(f for f in os.listdir(src_dir) if f.endswith(".py") and f not in EXCLUDE_SYNC)


def _parse_direction(argv):
    """→ (src_side, tgt_side, why) 或 (None, None, 原因)"""
    d = None
    for i, a in enumerate(argv):
        if a == "--direction" and i + 1 < len(argv):
            d = argv[i + 1]
        elif a.startswith("--direction="):
            d = a.split("=", 1)[1]
    frm = to = None
    for i, a in enumerate(argv):
        if a == "--from" and i + 1 < len(argv):
            frm = argv[i + 1]
        elif a.startswith("--from="):
            frm = a.split("=", 1)[1]
        elif a == "--to" and i + 1 < len(argv):
            to = argv[i + 1]
        elif a.startswith("--to="):
            to = a.split("=", 1)[1]
    if d:
        # ★ 记号语义（⛔ 第一版写反过、被自己的负例抓到）：
        #   `A<-B` = **以 B 为源、同步到 A**（左目标／右源）；`B->A` 才是左源／右目标。
        if "<-" in d:
            tgt, src = [x.strip() for x in d.split("<-", 1)]
        elif "->" in d:
            src, tgt = [x.strip() for x in d.split("->", 1)]
        elif "→" in d:
            src, tgt = [x.strip() for x in d.split("→", 1)]
        else:
            return None, None, "看不懂 --direction：%r（如 addon<-root 或 root->addon）" % d
        if src not in SIDES or tgt not in SIDES or src == tgt:
            return None, None, "看不动的方向名：%r（只认 addon / root，且两侧不同）" % d
        return src, tgt, "显式 --direction %s<-%s" % (tgt, src)
    if frm or to:
        if frm not in SIDES or to not in SIDES or frm == to:
            return None, None, "--from/--to 不合法：%r → %r" % (frm, to)
        return frm, to, "显式 --from %s --to %s" % (frm, to)
    return None, None, "未指定方向 ⇒ 自动判"


def _mtimes(src_dir, tgt_dir, names):
    newer_src, newer_tgt, same = [], [], []
    for n in names:
        s, t = os.path.join(src_dir, n), os.path.join(tgt_dir, n)
        if not os.path.isfile(t):
            continue
        if filecmp.cmp(s, t, shallow=False):
            same.append(n)
            continue
        if os.path.getmtime(s) > os.path.getmtime(t):
            newer_src.append(n)
        elif os.path.getmtime(t) > os.path.getmtime(s):
            newer_tgt.append(n)
        else:
            newer_src.append(n)          # mtime 相同而内容不同 ⇒ 无从判，按"源更新"记账并在判据里说明
    return newer_src, newer_tgt, same


def main(argv):
    check_only = "--check" in argv

    # ★ 先做一次**对称一致性预检**（2026-09-17）：两份已经一致时，⛔ 不该再去"判方向"——
    #   否则会出现「一致却报判不出（exit 3）」的假失败（实测：同步完再跑 --check 就撞到）。
    both = sorted(set(os.listdir(ADDON)) & set(os.listdir(ROOT)))
    both = [n for n in both if n.endswith(".py") and n not in EXCLUDE_SYNC]
    diff_now = [n for n in both if not filecmp.cmp(os.path.join(ADDON, n), os.path.join(ROOT, n),
                                                   shallow=False)]
    addon_only = sorted(n for n in os.listdir(ADDON)
                        if n.endswith(".py") and n not in EXCLUDE_SYNC and not os.path.isfile(os.path.join(ROOT, n)))
    if not diff_now and not addon_only:
        print("方向：无需判定（两侧已一致）")
        print("共同模块 %d 个：一致 %d ／ 分叉 0 ／ addon 独有 0" % (len(both), len(both)))
        # ★ 2026-09-18：把**根侧独有**（= 单侧件）也**打出来** —— 它们按设计**不参与**"两份一致"检查，
        #   但 ⛔ **不许变成「检查看不见」的静默豁免**：已声明的逐条给理由，未声明的显式标 ⚠。
        root_only = sorted(n for n in os.listdir(ROOT)
                           if n.endswith(".py") and n not in EXCLUDE_SYNC
                           and not os.path.isfile(os.path.join(ADDON, n)))
        declared = [n for n in root_only if n in SINGLE_SIDE_ROOT]
        undeclared = [n for n in root_only if n not in SINGLE_SIDE_ROOT]
        print("根侧独有 %d 个（**单侧件**，按设计不参与两侧一致检查）：已声明 %d ／ ⚠ 未声明 %d"
              % (len(root_only), len(declared), len(undeclared)))
        for n in declared:
            print("   · 已声明 %s —— %s" % (n, SINGLE_SIDE_ROOT[n]))
        for n in undeclared:
            print("   ⚠ 未声明 %s —— 历史既有单侧件（不影响本次一致性判定；建议后续逐条声明并写理由）" % n)
        print("结果：两份一致 ✓")
        return 0

    src_side, tgt_side, why = _parse_direction(argv)

    # ---- 自动判方向：拿"两侧都存在的同名文件"的 mtime 比 ----
    if src_side is None:
        probe = sorted(set(os.listdir(ADDON)) & set(os.listdir(ROOT)))
        probe = [n for n in probe if n.endswith(".py") and n not in EXCLUDE_SYNC]
        a_new, r_new, _ = _mtimes(ADDON, ROOT, probe)
        a_only = [n for n in a_new if n not in r_new]
        r_only = [n for n in r_new if n not in a_new]
        if r_only and not a_only:
            src_side, tgt_side, why = "root", "addon", "自动判：分叉文件都是**根侧更新**（%d 个）" % len(r_only)
        elif a_only and not r_only:
            src_side, tgt_side, why = "addon", "root", "自动判：分叉文件都是**addon 侧更新**（%d 个）" % len(a_only)
        else:
            print("方向：**判不出** —— %s" % why)
            print("  双向都有更新（或两边都没有）：addon 侧更新 %s ／ 根侧更新 %s"
                  % (a_only or "无", r_only or "无"))
            print("  ⇒ 必须显式给方向：`--direction addon<-root`（以根为准）或 `--direction root<-addon`。")
            return 3

    src_dir, tgt_dir = SIDES[src_side], SIDES[tgt_side]
    print("方向：源=%s  →  目标=%s" % (src_side, tgt_side))
    print("判据：%s" % why)
    print("      （读数：源 mtime 与目标 mtime 逐文件比；mtime 相同时按 mtime 判、内容不同计为目标需同步）")

    names = _shared_files(src_dir)
    missing, differ, newer, ok = [], [], [], 0
    # ★ 源侧独有（目标侧没有）的模块要分方向处置（2026-09-17 实测）：
    #   根侧比 addon 侧多 **16 个**（`build_final` / `make_icons` / `_scan_*` / `add_recoil_point` /
    #   `cab_check` … 都是 `--add-data` 给 **exe** 用的产品/开发脚本）⇒ 按根→addon 同步时
    #   ⛔ **不能**把它们注入 addon（会污染插件 zip；且 addon 侧根本不 import 它们）。
    #   反过来 addon→根 时，addon 独有的模块**必须**补到根侧（这正是本工具立项要保的不变量）。
    skip_src_only = (tgt_side == "addon")
    src_only = []
    for n in names:
        s = os.path.join(src_dir, n)
        t = os.path.join(tgt_dir, n)
        if not os.path.isfile(t):
            if skip_src_only:
                src_only.append(n)
            else:
                missing.append(n)
        elif not filecmp.cmp(s, t, shallow=False):
            # ⛔ 目标比源头**新**时拒绝自动覆盖：本脚本曾把「改在目标副本上的修复」
            #    直接吃掉（实测发生一次）✗。这种情况下必须人工确认哪份才是新的。
            if os.path.getmtime(t) > os.path.getmtime(s) + 1:
                newer.append(n)
            else:
                differ.append(n)
        else:
            ok += 1
    print("源头 %s" % src_dir)
    print("目标 %s" % tgt_dir)
    print("共同模块 %d 个：一致 %d / 待补到目标 %d / 内容不同 %d / **目标更新** %d / 源侧独有跳过 %d"
          % (len(names), ok, len(missing), len(differ), len(newer), len(src_only)))
    for n in missing:
        print("   [缺失] %s" % n)
    for n in src_only:
        print("   [源侧独有·跳过] %s（⛔ 不注入 %s）" % (n, tgt_side))
    for n in differ:
        print("   [分叉] %s  (源 %d 字节 / 目标 %d 字节)"
              % (n, os.path.getsize(os.path.join(src_dir, n)),
                 os.path.getsize(os.path.join(tgt_dir, n))))
    for n in newer:
        print("   [目标更新·拒绝覆盖] %s  (源 %s / 目标 %s)"
              % (n, time.strftime("%m-%d %H:%M:%S",
                                  time.localtime(os.path.getmtime(os.path.join(src_dir, n)))),
                 time.strftime("%m-%d %H:%M:%S",
                               time.localtime(os.path.getmtime(os.path.join(tgt_dir, n))))))
    if newer:
        print("!! 目标（%s）那份更新 —— ⛔ 拒绝自动覆盖。" % tgt_side)
        print("   若目标那份才是新的，请**反向**跑一次（当前方向 %s<-%s ⇒ 反过来是 %s<-%s），"
              "或人工比对后再定。" % (tgt_side, src_side, src_side, tgt_side))
        return 3
    if not missing and not differ:
        print("结果：两份一致 ✓")
        return 0
    if check_only:
        print("结果：**发现分叉** —— 跑 `python _sync_revtools.py --direction %s<-%s` 同步，"
              "或确认到底哪一份才是新的" % (tgt_side, src_side))
        return 2
    for n in missing + differ:
        shutil.copy2(os.path.join(src_dir, n), os.path.join(tgt_dir, n))
        print("   已同步 -> %s（%s → %s）" % (n, src_side, tgt_side))
    print("结果：已按方向 %s<-%s 同步 %d 个文件 ✓" % (tgt_side, src_side, len(missing) + len(differ)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
