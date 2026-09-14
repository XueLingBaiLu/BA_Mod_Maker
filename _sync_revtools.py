# -*- coding: utf-8 -*-
r"""**单一源头同步 + 分叉检测**：`blender_addon/_rev_tools` ↔ 根目录 `_rev_tools`。

为什么需要它（v1.8.60）：
  两个 `_rev_tools` 目录曾经**各自演化** ⇒ 实测出现分叉：
  `hub_edit.py` 12554 vs 18896 字节、`copy_full.py` 14543 vs 28922 字节、
  `compute_bundle_crc.py` 少一个 `import re`（跑 `compute_dir` 必崩 ✗）。
  同名的运行期模块在两处不一致，就会复现「改了一处、另一处还是旧的」这类最难查的 bug。
  ⇒ 定规则：**`blender_addon/_rev_tools` 是运行期模块的唯一源头**（它是随插件 zip 分发、
  也是所有测试实际 import 的那一份）；根目录那份由本脚本同步。
  根目录多出来的开发脚本（build_final / make_icons / _scan_* …）**不动**。

用法：
    python _sync_revtools.py            # 同步（addon → 根目录），并报告差异
    python _sync_revtools.py --check    # 只检查，有分叉返回码 2（构建/CI 用）
"""
import filecmp
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "blender_addon", "_rev_tools")
TARGET = os.path.join(HERE, "_rev_tools")

# 运行期模块：两份必须逐字节一致（插件与 exe 都会用到）
SHARED = "*.py"
# 只在源头存在、不需要同步过去的（无）——保留开关，将来要排除某些文件时用
EXCLUDE_SYNC = set()


def _shared_files():
    if not os.path.isdir(SOURCE):
        raise SystemExit("找不到源头目录：%s" % SOURCE)
    names = sorted(f for f in os.listdir(SOURCE)
                   if f.endswith(".py") and f not in EXCLUDE_SYNC)
    return names


def main(argv):
    check_only = "--check" in argv
    names = _shared_files()
    missing, differ, newer, ok = [], [], [], 0
    for n in names:
        s = os.path.join(SOURCE, n)
        t = os.path.join(TARGET, n)
        if not os.path.isfile(t):
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
    print("源头 %s" % SOURCE)
    print("目标 %s" % TARGET)
    print("共同模块 %d 个：一致 %d / 缺失 %d / 内容不同 %d / **目标更新** %d"
          % (len(names), ok, len(missing), len(differ), len(newer)))
    for n in missing:
        print("   [缺失] %s" % n)
    for n in differ:
        print("   [分叉] %s  (源 %d 字节 / 目标 %d 字节)"
              % (n, os.path.getsize(os.path.join(SOURCE, n)),
                 os.path.getsize(os.path.join(TARGET, n))))
    for n in newer:
        print("   [目标更新·拒绝覆盖] %s  (源 %s / 目标 %s)"
              % (n, time.strftime("%m-%d %H:%M:%S",
                                  time.localtime(os.path.getmtime(os.path.join(SOURCE, n)))),
                 time.strftime("%m-%d %H:%M:%S",
                               time.localtime(os.path.getmtime(os.path.join(TARGET, n))))))
    if newer:
        print("!! 有文件的目标副本更新 —— 说明有人改错了地方（应该只改 addon 那份）。")
        print("   请人工比对：若目标那份才是新的，把它复制回 %s 再重跑。" % SOURCE)
        return 3
    if not missing and not differ:
        print("结果：两份一致 ✓")
        return 0
    if check_only:
        print("结果：**发现分叉** —— 跑 `python _sync_revtools.py` 同步，"
              "或确认到底哪一份才是新的")
        return 2
    for n in missing + differ:
        shutil.copy2(os.path.join(SOURCE, n), os.path.join(TARGET, n))
        print("   已同步 -> %s" % n)
    print("结果：已从源头同步 %d 个文件到根目录 ✓" % (len(missing) + len(differ)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
