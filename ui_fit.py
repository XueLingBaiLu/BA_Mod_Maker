# -*- coding: utf-8 -*-
r"""全局窗口自适应：任何 Toplevel / 主窗都保证"内容放得下、按钮不被裁"。

⛔ 为什么需要（用户实测两次）：
  1. 「导入音频/音效…」底部按钮行在高 DPI 下被挤出窗口 ⇒ **必须手动拉伸窗口**才看得到；
  2. 逐个窗口打补丁的代价是"改一个漏一个"，新写的对话框还会再犯。
这里改成**一次性全局机制**：给 Tk 装一个 `<Map>` 钩子，任何窗口第一次显示时按
`winfo_reqwidth/reqheight`（内容的真实像素需求）**自动长到够用**，并把该尺寸设为
`minsize` ⇒ 窗口再也不可能被缩到裁掉按钮的程度。之后新建的对话框无需任何改动即受保护。

只增不减：用户手动放大的窗口不会被缩小；已有 `geometry()` 偏大时保持原样。
上限 = 屏幕的 `cap`（默认 92%），避免超出屏幕导致窗口标题栏/任务栏点不到。

用法（主程序只需两行）：
    from ui_fit import install_autofit
    install_autofit(self.root)          # __init__ 里调用一次
单独给某个窗口强制适配：`fit_window(win)`。
"""
from __future__ import annotations

import tkinter as tk

#: 已装过钩子的 root（防止重复绑定导致重复适配）
_installed: set[str] = set()


def _palette_bg(win, default="#000000"):
    r"""取主窗调色板的背景色（拿不到就返回 default）。"""
    try:
        master = win.master
        while master is not None and not hasattr(master, "c"):
            master = getattr(master, "master", None)
        if master is not None and isinstance(getattr(master, "c", None), dict):
            return master.c.get("bg", default)
    except Exception:  # noqa: BLE001
        pass
    return default


def fit_window(win, cap=0.92, grow_only=True, set_minsize=True):
    r"""把窗口长到"内容放得下"。返回 (宽, 高)。

    * `winfo_reqheight()` 必须在 `update_idletasks()` **之后**读，否则拿到 1。
    * 只增不减（`grow_only=True`）：用户手动放大的尺寸会被保留。
    * `set_minsize=False` 时**不锁下限** —— ⛔ v1.8.65 教训：给主窗/对话框锁
      `minsize(内容需求)` 会让用户**无法缩回窗口**，一旦尺寸不合适就只能看着坏界面。
      只有尺寸完全可控的对话框才考虑锁下限。
    """
    result = None
    try:
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        max_w, max_h = max(int(sw * cap), 400), max(int(sh * cap), 300)
        need_w = max(int(win.winfo_reqwidth()), 1)
        need_h = max(int(win.winfo_reqheight()), 1)
        cur_w = int(win.winfo_width()) if win.winfo_ismapped() else 0
        cur_h = int(win.winfo_height()) if win.winfo_ismapped() else 0
        new_w = min(max(need_w, cur_w if grow_only else 0), max_w)
        new_h = min(max(need_h, cur_h if grow_only else 0), max_h)
        if new_w != cur_w or new_h != cur_h:
            win.geometry("%dx%d" % (new_w, new_h))
        if set_minsize:
            win.minsize(min(need_w, max_w), min(need_h, max_h))
        result = (new_w, new_h)
    except Exception:  # noqa: BLE001 - 适配失败不影响功能
        pass
    return result


def make_flow(container, padx=3, pady=1, slack=24):
    r"""把容器的子控件改成**流式布局**：够宽就一行，不够就自动换行。

    ⛔ 为什么需要：Tk 的 pack/grid 在容器**装不下**时不会映射放不下的控件
    （比"被裁掉"更隐蔽 —— 用户连拉伸窗口都看不到它们）。实测：详情编辑器顶部
    按钮行在 267% 缩放下需要 2922px、主窗工具栏同理，末尾的按钮直接消失。
    流式布局保证任何缩放下**每个控件都在**（必要时折到下一行），配合
    `install_autofit` 的窗口自适应长高，永远不会"少按钮"。

    自动换行后还会调用 `fit_window` 让窗口长高，避免第二行被裁。
    """
    state = {"rows": 0, "frames": []}

    def relayout(_event=None):
        # ⛔ 先把上一轮生成的行 Frame 拆掉，**再**取子控件列表：否则它们会被
        #    当成普通控件再次参与排版（层层嵌套）→ 界面错乱。
        #    v1.8.65 就是这个顺序写反了（用户实测"界面完全混乱"）。
        for rf in list(state["frames"]):
            try:
                rf.destroy()
            except Exception:  # noqa: BLE001
                pass
        state["frames"] = []
        try:
            kids = [w for w in container.winfo_children() if w.winfo_manager()]
        except Exception:  # noqa: BLE001
            return
        if not kids:
            return
        try:
            avail = container.winfo_width()
            if avail <= 1:
                avail = max(container.winfo_reqwidth(), 200)
            widths = [max(w.winfo_reqwidth() + 2 * padx, 1) for w in kids]
            # 一行放得下就一行；放不下才换行（留 slack 迟滞，避免反复抖动）
            rows, cur, curw = [], [], 0
            for w, wd in zip(kids, widths):
                if cur and curw + wd + slack > avail:
                    rows.append(cur)
                    cur, curw = [], 0
                cur.append(w)
                curw += wd
            if cur:
                rows.append(cur)
            if state["rows"] == len(rows):
                # 行数没变也要把行 Frame 建回来（上面刚拆掉）
                pass
            state["rows"] = len(rows)
            for w in kids:
                try:
                    w.pack_forget()
                    w.grid_forget()
                except Exception:  # noqa: BLE001
                    pass
            # ⛔ 必须"每行一个子 Frame + pack"，**不能用同一个 grid**：
            #    grid 的列是跨行共享的，换行后容器的 reqwidth 仍是各列之和 → 宽度一点没减
            #    （实测踩过：换成 grid 多行后 req 依旧 2145px，按钮照样出界）。
            for row in rows:
                rf = tk.Frame(container)
                try:
                    rf.config(bg=container.cget("bg"))
                except Exception:  # noqa: BLE001
                    pass
                rf.pack(side="top", fill="x", anchor="w")
                state["frames"].append(rf)
                for w in row:
                    w.pack(in_=rf, side="left", padx=padx, pady=pady)
            if len(rows) > 1:
                # 换行后容器变高 ⇒ 让窗口长高，否则新的一行会被裁
                try:
                    fit_window(container.winfo_toplevel(), set_minsize=False)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - 布局失败不影响功能
            pass

    try:
        container.bind("<Configure>", relayout, add="+")
        container.after(80, relayout)
    except Exception:  # noqa: BLE001
        pass
    return relayout


def install_autofit(root, cap=0.90):
    r"""给 root 装全局自适应钩子：**仅对对话框（Toplevel）**在首次显示时放大到够用。

    ⛔ v1.8.65 教训（**用户实测"界面完全混乱、缺失严重"**）：当时的实现还
      ① 对**主窗口**也做自适应并 `geometry()` 放大 → 主窗被撑到超出屏幕，
         大片界面跑到屏幕外；
      ② 一律 `minsize(内容需求)` → 用户**连手动缩回来都做不到**，只能看着坏界面。
      所以现在：
      - **不碰主窗**（用户自己调好的窗口尺寸一律不动）；
      - **不设 minsize**（绝不锁死用户的手动调整）；
      - 只对**对话框**做"只增不减"的放大，并限制在屏幕 90% 以内。
    真正需要"按钮永远可见"的对话框，仍应显式调用 `fit_and_center` / 底部先占位。
    """
    key = str(root)
    if key in _installed:
        return False
    _installed.add(key)

    def _on_map(event):
        w = event.widget
        if not isinstance(w, tk.Toplevel) or w is root:
            return                      # 子控件事件、以及主窗，一律跳过
        if not w.winfo_ismapped():
            return
        try:
            fit_window(w, cap=cap, set_minsize=False)
        except Exception:  # noqa: BLE001
            pass

    try:
        root.bind_all("<Map>", _on_map, add="+")
    except Exception:  # noqa: BLE001
        return False
    return True


# ====================================================================== 深色主题

#: ⛔ **兜底**深色配色（只在拿不到主程序调色板时用）。
#: 主程序是 dark-only（`ba_db_tool.EditorApp.c`），生产路径一定会拿到真调色板 ⇒
#: 这份只用于"对话框被单独起来跑"（自测脚本、插件内嵌）时**别让经典控件变回白底**。
#: 判据在 `测试\test_dialog_dark_theme.py`（含负向对照）。
DARK_FALLBACK = {
    "bg": "#000000", "panel": "#000000", "fg": "#b8cdff",
    "entry_bg": "#0c111c", "entry_fg": "#cfe0ff",
    "btn_bg": "#0c1016", "btn_fg": "#b8cdff", "btn_active": "#1c2b3f",
    "sel": "#1f5fbf", "sel_fg": "#ffffff",
    "list_bg": "#000000", "list_fg": "#cfe0ff",
    "accent": "#5b9dff", "disabled": "#5c6b84",
}

#: 经典控件类名 → 需要设置的颜色（ttk 控件由全局 `Style` 管，这里**不碰**）
_CLASSIC_ROLES = {
    "Text": ("entry_bg", "entry_fg"),
    "Listbox": ("list_bg", "list_fg"),
    "Entry": ("entry_bg", "entry_fg"),
}


def palette_of(master, dlg=None):
    r"""从主窗链上取主程序的调色板字典 `app.c`；取不到就返回 `DARK_FALLBACK`。

    ⛔ 坑：要**沿着 `master` 链往上找**（对话框的 master 可能是 Tk 根窗口，
    而根窗口本身没有 `.c`，只有 `._ba_app`）。
    """
    seen = []
    for cand in (dlg, master, getattr(master, "master", None), getattr(dlg, "master", None)):
        if cand is None:
            continue
        seen.append(cand)
        app = getattr(cand, "_ba_app", None)
        if app is not None:
            seen.append(app)
    cur = master
    while cur is not None:
        seen.append(cur)
        cur = getattr(cur, "master", None)
    for cand in seen:
        c = getattr(cand, "c", None)
        if isinstance(c, dict) and c.get("bg"):
            return c
    return DARK_FALLBACK


def find_app(master, dlg=None):
    r"""找带 `theme_children()` 的主程序对象（找不到返回 None）。"""
    for cand in (master, getattr(master, "master", None), getattr(dlg, "master", None)):
        if cand is None:
            continue
        if callable(getattr(cand, "theme_children", None)):
            return cand
        inner = getattr(cand, "_ba_app", None)
        if inner is not None and callable(getattr(inner, "theme_children", None)):
            return inner
    return None


def paint_classic(widget, palette=None):
    r"""递归给**经典 tk 控件**上色（`tk.Text` / `tk.Listbox` / `tk.Entry` / `tk.Frame` …）。

    ⛔ 为什么必须有这一步：`ttk` 控件由全局 `Style` 统一上色，但
    **`tk.Text`、`tk.Listbox` 这类经典控件不跟随 ttk 主题**，默认是刺眼的
    `SystemWindow`（白）—— 用户实测反馈："「我的 bundle」② 和 ④ 的显示栏白底太刺眼"。
    """
    c = palette or DARK_FALLBACK
    if not isinstance(c, dict):
        c = DARK_FALLBACK
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            cls = w.winfo_class()
        except Exception:  # noqa: BLE001
            continue
        try:
            if cls == "Toplevel":
                w.config(bg=c.get("bg", "#000000"))
            elif cls == "Text":
                w.config(bg=c["entry_bg"], fg=c["entry_fg"], insertbackground=c["entry_fg"],
                         selectbackground=c.get("sel"), selectforeground=c.get("sel_fg"),
                         highlightbackground=c.get("panel"), highlightcolor=c.get("accent"))
            elif cls == "Listbox":
                w.config(bg=c["list_bg"], fg=c["list_fg"],
                         selectbackground=c.get("sel"), selectforeground=c.get("sel_fg"),
                         highlightbackground=c.get("panel"), highlightcolor=c.get("accent"))
            elif cls == "Entry":
                w.config(bg=c["entry_bg"], fg=c["entry_fg"], insertbackground=c["entry_fg"],
                         highlightbackground=c.get("panel"), highlightcolor=c.get("accent"))
            elif cls == "Canvas":
                w.config(bg=c.get("bg"))
            elif cls in ("Frame", "Labelframe"):
                w.config(bg=c.get("bg"))
            elif cls == "Panedwindow":
                w.config(bg=c.get("bg"))
            elif cls in ("Button",):
                w.config(bg=c["btn_bg"], fg=c["btn_fg"],
                         activebackground=c["btn_active"], activeforeground=c["btn_fg"],
                         disabledforeground=c.get("disabled"))
            elif cls == "Label":
                role = getattr(w, "_color_role", None)
                w.config(bg=c.get("bg"), fg=c.get(role) if role else c["fg"])
        except Exception:  # noqa: BLE001 - 个别控件的选项不支持就算了
            pass
        try:
            stack.extend(w.winfo_children())
        except Exception:  # noqa: BLE001
            pass


def theme_from_master(dlg, master):
    r"""让对话框继承主程序配色 —— **保证经典控件不再是刺眼白底**。

    两级保险：
      ① 有主程序对象 ⇒ 调它的 `theme_children()`（唯一权威，含 `_color_role` 之类的细节）；
      ② 无论①成不成功，都再按调色板给经典控件兜一遍底（幂等）。
    """
    app = find_app(master, dlg)
    if app is not None:
        try:
            app.theme_children(dlg)
        except Exception:  # noqa: BLE001 - 主题失败不影响功能
            pass
    paint_classic(dlg, palette_of(master, dlg))
    return app is not None

