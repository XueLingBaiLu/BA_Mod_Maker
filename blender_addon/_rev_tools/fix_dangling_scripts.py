# -*- coding: utf-8 -*-
r"""**【发布-03】把被引用的 MonoScript 复制进自建包** —— 修"prefab 脚本全失效"。

为什么需要它（2026-10 第 72 轮实机定案）
========================================
从游戏包里把 prefab 复制进自建包时，**只复制了对象、没复制它引用的 MonoScript** ⇒
134 个 MonoBehaviour 的 `m_Script`（`m_FileID = 0` = **本文件内** + pathID）一个都查不到 ⇒
* 游戏里刷一大串 `The referenced script on this Behaviour (Game Object 'US_ACV') is missing!`
* 场景编辑器直接抛 `System.ArgumentException: No UnitPrefabRoot script found on unit prefab '…'`
* ⚠ 最阴的：Unity 缺脚本时**照样渲染静态网格/材质** ⇒ 战场上"看着成功了"，
  但 `UnitPrefabRoot`（炮塔配对）、`AnimationHub`、能力挂点等**全部静默失效** ✗

机制：Unity 打包时会把"本包用到的 MonoScript"**复制一份进包**（所以 `m_FileID=0` 才解析得到）。

用法
====
    # ① 只查（不改东西）：和 `bundle_script_check.py` 同口径
    python 技术资料\scripts\fix_dangling_scripts.py --bundle <bundle>

    # ② 修：把源包里同 pathID 的 MonoScript 复制进来，另存新包
    python 技术资料\scripts\fix_dangling_scripts.py --bundle <bundle> \
        --source <含这些 MonoScript 的包/._assets> --out <新包>

    # ③ 造包时自动带上（产品流程里就是这么调的）：
    from fix_dangling_scripts import copy_referenced_monoscripts
    n, missing = copy_referenced_monoscripts(dst_sf, src_sf)

输出怎么读
==========
* `需要 N 个 m_Script · 包内可解析 K / N` ⇒ 修完必须 **K == N**（`0/N` = 全靠外部、必失效）
* `✓ 复制了 X 个 MonoScript（保持原 pathID）` ⇒ 这一步就是修复本体
* `⚠ 源包里也找不到 Y 个` ⇒ 换一个更"原始"的源包（通常是**该 prefab 原来所在的那个包**）⚠

坑
==
* **必须保持原 pathID**：`m_FileID=0` + pathID 才能解析 ⇒ 不能像普通导入那样重新分配 pid ✗
* `MonoScript` 很小（只有类名/命名空间/程序集/属性哈希），复制它**不会**把包撑大
* 源包要选对：本例那 16 个 pathID **不在** `globalgamemanagers.assets`（实测 0/4 命中）——
  它们是**按包分配的**，所以要从 **prefab 原来那个包**（或那次导入的源包）里取 ✓
"""
import argparse
import os
import sys

try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(WS, "工具制作资源", "BA_Mod_Maker", "blender_addon", "_rev_tools"),
          os.path.join(WS, "工具制作资源", "BA_Mod_Maker", "_rev_tools")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


def script_stats(sf):
    r"""★ 一次遍历把"脚本引用"全貌数清楚（**只读**）—— 自检和修都用它，口径只有一份 ✓

    返回:
        {
          "mb":               MonoBehaviour 个数,
          "unreadable":       读不出 typetree 的个数（**不能当成"没问题"**）,
          "internal":         `m_FileID == 0`（本文件内引用）的个数,
          "external":         `m_FileID != 0`（**跨文件**引用）的个数,
          "need":             {pathID: 用到它的 MonoBehaviour 数}（只含 internal）,
          "missing":          [本文件内也找不到的 pathID...],
          "external_targets": {m_FileID: 个数}（外部引用指向哪个文件 —— 只报数，不判对错）,
        }

    ★ 2026-10-15（用户现场 ③ 冒假绿）：以前只数 `m_FileID == 0`，**外部引用被静默丢掉** ⇒
      万一包里的 MonoBehaviour 引用的是**别的文件**，老代码会显示"✓ 全在本包内"——
      **那是个假绿** ✗。现在 `external` 明确数出来并打进自检输出，
      让"检查了 0 条引用"和"检查了 512 条引用全过"**在输出上分得开** ✓
    """
    import collections
    st = {"mb": 0, "unreadable": 0, "internal": 0, "external": 0,
          "need": collections.Counter(), "missing": [],
          "external_targets": collections.Counter()}
    have = set(getattr(sf, "objects", None) or {})
    for o in list((getattr(sf, "objects", None) or {}).values()):
        if getattr(getattr(o, "type", None), "name", "") != "MonoBehaviour":
            continue
        st["mb"] += 1
        try:
            d = o.read_typetree()
        except Exception:                                       # noqa: BLE001
            st["unreadable"] += 1
            continue
        s = d.get("m_Script") or {}
        fid = s.get("m_FileID", 0)
        if fid != 0:
            st["external"] += 1
            st["external_targets"][fid] += 1
            continue
        st["internal"] += 1
        st["need"][s.get("m_PathID", 0)] += 1
    st["missing"] = [p for p in st["need"] if p not in have]
    return st


def needed_scripts(sf):
    """返回 {m_Script pathID: 用到它的 MonoBehaviour 数}（只统计 `m_FileID == 0` 的）"""
    return script_stats(sf)["need"]


def copy_referenced_monoscripts(dst_sf, src_sf, log=print):
    """把 `dst_sf` 里 MonoBehaviour 引用、但**本文件内不存在**的 MonoScript 从 `src_sf` 复制过来。

    **保持原 pathID**（`m_FileID=0` 的引用靠它解析）⇒ 用 `ObjectReader` 原样写一份。
    返回 (复制个数, 仍缺的 pathID 列表)。
    """
    from UnityPy.files.ObjectReader import ObjectReader
    need = needed_scripts(dst_sf)
    have = set(dst_sf.objects.keys())
    missing = [p for p in need if p not in have]
    if not missing:
        log("✓ 没有悬空脚本（%d 个 m_Script 全在本文件内）" % len(need))
        return 0, []
    src_objs = getattr(src_sf, "objects", {})
    copied, still = 0, []
    for pid in missing:
        src = src_objs.get(pid)
        if src is None or getattr(getattr(src, "type", None), "name", "") != "MonoScript":
            still.append(pid)
            continue
        try:
            obj = src.read()
            r = ObjectReader(assets_file=dst_sf, reader=dst_sf.reader, path_id=pid,
                             type_id=src.type_id, serialized_type=src.serialized_type,
                             class_id=src.class_id, type=src.type,
                             byte_start=src.byte_start, byte_size=0,
                             is_destroyed=src.is_destroyed, is_stripped=src.is_stripped)
            r.save_typetree(obj)                                # ★ 保持原 pathID 写回
            dst_sf.objects[pid] = r
            copied += 1
        except Exception as e:                                  # noqa: BLE001
            log("  ⚠ 复制 pathID=%s 失败：%s" % (pid, e))
            still.append(pid)
    log("✓ 复制了 %d 个 MonoScript（保持原 pathID）；仍缺 %d 个" % (copied, len(still)))
    if still:
        log("  ⚠ 源包里也找不到这些 pathID ⇒ 换一个更原始的源包（prefab 原来所在的包）⚠")
    return copied, still


def _load_source(path):
    import UnityPy
    env = UnityPy.load(path)
    objs = list(env.objects)
    if not objs:
        raise RuntimeError("源文件里没有对象：%s" % path)
    return env, objs[0].assets_file


def main():
    ap = argparse.ArgumentParser(description="把被引用的 MonoScript 复制进包（修悬空脚本）")
    ap.add_argument("--bundle")
    ap.add_argument("--source", help="含这些 MonoScript 的包或 .assets 文件（修的时候必须给）")
    ap.add_argument("--out", help="修完写到哪（不给就只查）")
    ap.add_argument("--inner", help="只处理包内这个内部文件（默认全部）")
    a = ap.parse_args()
    if not a.bundle or not os.path.isfile(a.bundle):
        print("✗ 找不到 %s" % a.bundle)
        return 2

    import UnityPy
    env = UnityPy.load(a.bundle)
    targets = []
    for name, f in (env.file.files or {}).items():
        if a.inner and name != a.inner:
            continue
        sf = f if hasattr(f, "objects") else None
        if sf is not None and getattr(sf, "objects", None):
            targets.append((name, sf))
    if not targets:
        print("✗ 包里读不到可解析的内部文件")
        return 2

    total_need = 0
    for name, sf in targets:
        need = needed_scripts(sf)
        have = set(sf.objects.keys())
        ok = [p for p in need if p in have]
        total_need += len(need)
        print("内部文件 %s：MonoBehaviour 引用 %d 个 m_Script · 包内可解析 %d / %d"
              % (name, sum(need.values()), len(ok), len(need)))
    if not a.source or not a.out:
        print("\n（只查模式：给 `--source` 和 `--out` 才会修）")
        return 0

    _env2, src_sf = _load_source(a.source)
    for name, sf in targets:
        copy_referenced_monoscripts(sf, src_sf)
    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(a.out, "lz4")
    print("✓ 写出：%s" % a.out)

    env3 = UnityPy.load(a.out)
    bad = 0
    for name, f in (env3.file.files or {}).items():
        sf = f if hasattr(f, "objects") else None
        if sf is None or not getattr(sf, "objects", None):
            continue
        need = needed_scripts(sf)
        have = set(sf.objects.keys())
        miss = [p for p in need if p not in have]
        bad += len(miss)
        print("  复查 %s：需要 %d · 缺 %d" % (name, len(need), len(miss)))
    print("⇒ 复查结果：%s" % ("✅ 无悬空脚本 ✓" if bad == 0 else "⛔ 还有 %d 个缺 ✗" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
