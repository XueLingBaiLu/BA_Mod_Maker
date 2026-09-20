# -*- coding: utf-8 -*-
r"""**`[界面-01]` 真对话框像素级判据** —— ③ 新建 bundle / 加资产 区（`frm3`）里**有没有控件被同格盖住**。

为什么必须跑真对话框（工具优先）：静态探针 `probe_gui_grid_collisions.py` 只判"同 (row,column) 有 ≥2 个
控件"，**判不出观感**；而用户报的是"**完全不可见**"。⇒ 本探针**真起一个 `MyBundleDialog`**，
量每个控件的**实际像素矩形**，再按 tkinter 的叠放规则（**同父、后创建者在上**）算**遮挡比例**：
`遮挡比例 > 0` ⇒ 有控件被压住 ⇒ FAIL。

判据（`exit 0 = PASS / 1 = FAIL`）：
  A. `frm3` 内**任意两个控件矩形不相交**（遮挡比例全 0）；
  B. 5 组关键控件**都真实存在且可见**（`winfo_ismapped()==1` 且矩形 > 0）：
     `地址：`+输入框 / `📏 查 DB 原值` / `bundle 内路径：`+输入框 / `资产类型：`+下拉 / `把素材加进这个 bundle`；
  C. `地址` 输入框的**输入框本身没被任何控件覆盖**（用户报的那一条）；
  D. 可见提示文案里含「**地址留空**」（需求：写清"留空即不注册"；判据 `my_bundle.py:271 if catalog and address:`）。

用法：
    python _rev_tools\out\probe_mybundle_grid_overlap.py [--show]
⛔ 只读：只构建窗口、只读几何，**不写任何产品文件**、不点任何按钮。

局限（如实声明）：只量**本机 1000×900** 这一档；tkinter 的 `winfo_*` 只在窗口已 `update()` 后有值
⇒ 本探针先 `geometry` 再 `update()`。像素数依赖主题/DPI，**判据只用"是否相交（>0）"**，
⛔ 不拿绝对像素当阈值。
"""
import sys

PROD = r"<工作目录>\工具制作资源\BA_Mod_Maker"
if PROD not in sys.path:
    sys.path.insert(0, PROD)


def r_of(w):
    return (w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())


def inter(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x = max(ax, bx)
    y = max(ay, by)
    r = min(ax + aw, bx + bw)
    btm = min(ay + ah, by + bh)
    if r <= x or btm <= y:
        return 0
    return (r - x) * (btm - y)


def desc(w):
    cls = w.winfo_class()
    t = ""
    try:
        t = str(w.cget("text"))
    except Exception:                                                  # noqa: BLE001
        pass
    if not t:
        try:
            tv = str(w.cget("textvariable"))
            if tv.startswith("PY_VAR"):
                t = "<var>=%r" % (w.tk.globalgetvar(tv),)
        except Exception:                                              # noqa: BLE001
            pass
    return "%s %r" % (cls, t[:30])


def main(argv):
    show = "--show" in argv
    # `--neg <目录>`：负向对照 —— 把该目录插到 PROD **之前**，让 `import my_bundle` 取到旧版
    #（本机可复跑：`--neg 测试\_gate_tmp\_mut_base` = 148,178 B / sha16 `46F42DBAC67F5754` 改前版）。
    if "--neg" in argv:
        d = argv[argv.index("--neg") + 1]
        sys.path.insert(0, d)
        print("[负向对照] my_bundle 取自 %s" % d)
    import tkinter as tk
    import my_bundle
    print("[探针] my_bundle 实际加载自：%s" % getattr(my_bundle, "__file__", "?"))

    root = tk.Tk()
    root.geometry("100x100+0+0")
    root.update()
    dlg = my_bundle.MyBundleDialog(root)
    dlg._all_sections(True)
    win = dlg.win
    win.geometry("1000x900")
    win.update()
    win.update_idletasks()

    body = dlg.sec_assets.body
    kids = body.winfo_children()
    print("=" * 96)
    print("[界面-01] 真对话框像素判据 · 容器=③ 新建 bundle/加资产 的 body（frm3）· 子控件 %d 个" % len(kids))
    print("-" * 96)

    fails = []
    rects = [(w, r_of(w)) for w in kids]
    for w, (x, y, ww, hh) in rects:
        if show:
            gi = w.grid_info()
            print("  L?%-3s r=%-2s c=%-2s cs=%-2s x=%-4d y=%-4d w=%-4d h=%-3d mapped=%s  %s"
                  % (gi.get("row", "?"), gi.get("row", "?"), gi.get("column", "?"),
                     gi.get("columnspan", "?"), x, y, ww, hh, w.winfo_ismapped(), desc(w)))

    # ── A. 任意两控件矩形不相交（同父、后创建者在上 ⇒ 后盖前）────────────────
    print("\n【A】遮挡检查（同父、后创建者在上）")
    cover = {}
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            wi, ri = rects[i]
            wj, rj = rects[j]
            a = inter(ri, rj)
            if a > 0:
                area = max(ri[2] * ri[3], 1)
                cover.setdefault(wi, []).append((wj, a, a / area))
    if not cover:
        print("  ✓ 无任何控件矩形相交（遮挡比例全 0）")
    for w, lst in cover.items():
        for wj, a, frac in lst:
            print("  ⛔ %s\n       被（后创建）%s 遮挡 %d px² = 自身面积的 %.1f%%"
                  % (desc(w), desc(wj), a, frac * 100))
            fails.append("%s 被 %s 遮挡 %.1f%%" % (desc(w), desc(wj), frac * 100))

    # ── B/C. 5 组关键控件存在 + 地址框未被覆盖 ─────────────────────────────
    print("\n【B】5 组关键控件存在性/可见性")
    def find(cls, needle):
        out = []
        for w in kids:
            if w.winfo_class() != cls:
                continue
            t = ""
            try:
                t = str(w.cget("text"))
            except Exception:                                          # noqa: BLE001
                pass
            if needle in t:
                out.append(w)
        return out

    def entry_of(var):
        """按 **StringVar 对象本身**比对（⛔ 不能用属性名：`cget("textvariable")` 返回的是
        tk 内部名 `PY_VAR5`，不含 Python 属性名 ⇒ 第一版本探针据此误报"找不到输入框"）。"""
        want = str(var)
        for w in kids:
            if w.winfo_class() in ("TEntry", "Entry", "TCombobox"):
                try:
                    if str(w.cget("textvariable")) == want:
                        return w
                except Exception:                                      # noqa: BLE001
                    pass
        return None

    groups = [
        ("标签「地址：」", find("TLabel", "地址")),
        ("输入框 addr_var", [w for w in (entry_of(dlg.addr_var),) if w]),
        ("按钮「📏 查 DB 原值」", find("TButton", "查 DB 原值")),
        ("标签「bundle 内路径：」", find("TLabel", "bundle 内路径")),
        ("输入框 asset_var", [w for w in (entry_of(dlg.asset_var),) if w]),
        ("标签「资产类型：」", find("TLabel", "资产类型")),
        ("下拉 types_box", [dlg.types_box]),
        ("按钮「把素材加进这个 bundle」", find("TButton", "把素材加进这个 bundle")),
    ]
    for name, ws in groups:
        if not ws:
            print("  ⛔ %s ⇒ **找不到**" % name)
            fails.append("%s 找不到" % name)
            continue
        w = ws[0]
        x, y, ww, hh = r_of(w)
        ok = bool(w.winfo_ismapped()) and ww > 0 and hh > 0
        cov = cover.get(w)
        print("  %s %-26s x=%-4d y=%-4d w=%-4d h=%-3d mapped=%s 被遮挡=%s"
              % ("✓" if ok and not cov else "⛔", name, x, y, ww, hh, w.winfo_ismapped(),
                 "无" if not cov else "有"))
        if not ok:
            fails.append("%s 不可见/尺寸为 0" % name)
        if cov:
            fails.append("%s 被遮挡" % name)

    # ── D. 文案：「地址留空」必须出现在可见控件文本里 ─────────────────────
    print("\n【D】文案：写清「地址留空即不注册」")
    hits = []
    for w in kids:
        try:
            t = str(w.cget("text"))
        except Exception:                                              # noqa: BLE001
            continue
        if "地址留空" in t:
            hits.append((w, t))
    for w, t in hits:
        print("  ✓ 命中：%s ⇒ %r" % (w.winfo_class(), t[:70]))
    if not hits:
        print("  ⛔ 没有可见控件写着「地址留空」")
        fails.append("文案缺「地址留空」")

    win.destroy()
    root.destroy()
    print("=" * 96)
    if fails:
        for f in fails:
            print("  ⛔ %s" % f)
        print("结论：FAIL（%d 条）" % len(fails))
        return 1
    print("结论：PASS（无遮挡 ✓ 5 组控件均可见 ✓ 地址框未被覆盖 ✓ 文案含「地址留空」✓）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
