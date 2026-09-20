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

import ctypes
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
    # ★ 顺手把"最大化按钮"补回来（`transient()` 会把它抹掉，见 `allow_maximize`）——
    #   ⛔ 必须在这里也做一次：`install_autofit` 的 `<Map>` 钩子只覆盖"程序里装了钩子"的路径，
    #      而单独起的对话框（自测脚本 / 别的入口）只会在自己调 `fit_window` 时才经过这里 ✓
    try:
        allow_maximize(win)
    except Exception:  # noqa: BLE001
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


def allow_maximize(win, force=False):
    r"""★ 让窗口的**标题栏最大化按钮**真的能点（2026-10-15 用户要求："让所有新窗口能够最大化"）。

    ⛔ 真因（现场取证，不是猜）：**`wm_transient(True)` 会让 Windows 清掉 `WS_MAXIMIZEBOX`**
      ⇒ 标题栏那个最大化按钮**变灰**；而且 **`resizable(True, True)` 也救不回来** ✗
      实测（`tools\perf\probe_maximize2.py` 直接读 `GWL_STYLE`）：

      | 窗口 | resizable | 最大化框位 | 能最大化 |
      |---|---|---|---|
      | 普通 Toplevel | (1,1) | ✔ | **能** |
      | `transient(True)` | (1,1) | ✘ | **不能** |
      | `transient` + `resizable(True,True)` | (1,1) | ✘ | **不能** |
      | 产品各对话框（都调了 transient） | (1,1) | ✘ | **不能** |

    ⇒ 两条路：① 干脆不调 `transient()`（但对话框就不再"附属于父窗"，还会多一个任务栏按钮）；
      ② **保留 transient，把 `WS_MAXIMIZEBOX` 加回去** ← 采用这条（不牺牲对话框语义）✓

    ⛔ 两条纪律：
      · **必须在窗口映射之后**做：Tk 在 `transient()` / `deiconify()` 时会重设样式，
        提前设会被覆盖（所以主路径挂在 `<Map>` 钩子上，见 `install_autofit`）；
      · **尊重显式的 `resizable(False, False)`**（作者故意做成固定尺寸的提示框不要动它）——
        除非 `force=True`。非 Windows 平台直接跳过。
    """
    if not hasattr(ctypes, "windll"):
        return False
    try:
        if not force and tuple(win.resizable()) == (0, 0):
            return False                   # 作者故意固定尺寸 ⇒ 不碰
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        h = parent or hwnd                 # 标题栏在父窗上（Tk 的 winfo_id 是内层子窗）
        GWL_STYLE, WS_MAXIMIZEBOX, WS_THICKFRAME = -16, 0x00010000, 0x00040000
        st = ctypes.windll.user32.GetWindowLongW(h, GWL_STYLE)
        if st & WS_MAXIMIZEBOX:
            return False                   # 本来就能最大化
        ctypes.windll.user32.SetWindowLongW(
            h, GWL_STYLE, st | WS_MAXIMIZEBOX | WS_THICKFRAME)
        # 样式改了要通知系统重画边框，否则按钮状态不会立刻刷新
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        ctypes.windll.user32.SetWindowPos(
            h, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return True
    except Exception:  # noqa: BLE001 - 改不了就不改，别影响功能
        return False


def can_maximize(win):
    r"""→ 这个窗口现在**能不能**最大化（读 Win32 样式位，不是问 Tk）。

    真值判据（Windows 的规矩）：**`WS_MAXIMIZEBOX` 与 `WS_THICKFRAME` 必须同时存在**，
    只亮一个时系统会把最大化按钮**灰掉**。非 Windows / 读不到 ⇒ 返回 None（**不假装知道**）。
    """
    if not hasattr(ctypes, "windll"):
        return None
    try:
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        h = parent or hwnd
        st = ctypes.windll.user32.GetWindowLongW(h, -16)
        return bool(st & 0x00010000) and bool(st & 0x00040000)
    except Exception:  # noqa: BLE001
        return None


def apply_dark_title_bar(win, dark=True):
    r"""把窗口的**系统标题栏**切到暗色（Windows 10+ 的 DWM 沉浸式暗色）。

    为什么需要（用户 2026-09-19 亲令 `[界面-05]`）：ttk 主题只管**客户区**，
    而标题栏是 **DWM 画的**、不跟主题走 ⇒ 浅色系统主题下对话框顶部是一条**白条**。
    ★ 判据（**能失败**）：用 `title_bar_dark_state(win)` **回读** DWM 属性 ——
      改前（或显式 `dark=False`）读 **0**，改后读 **1**（`测试\test_dialog_dark_theme.py` ⑥）。
    ⛔ 非 Windows／调用失败 ⇒ 返回 `None`（**不假装成功**），且**绝不影响功能**。
    """
    if not hasattr(ctypes, "windll"):
        return None
    try:
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        h = parent or hwnd                 # 标题栏在父窗上（Tk 的 winfo_id 是内层子窗）
        value = ctypes.c_int(1 if dark else 0)
        rc = None
        for attr in (20, 19):              # DWMWA_USE_IMMERSIVE_DARK_MODE ／（旧版回退）
            rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                h, attr, ctypes.byref(value), ctypes.sizeof(value))
            if rc == 0:
                break
        return {"hwnd": h, "attr": attr, "rc": rc,
                "readback": title_bar_dark_state(win)}
    except Exception:  # noqa: BLE001 - 改不了就不改，别影响功能
        return None


def title_bar_dark_state(win):
    r"""→ 这个窗口的标题栏**现在**是不是暗色（读 DWM 属性，⛔ 不是问 Tk）。

    返回 `1`／`0`／`None`（`None` ＝ 读不到 ⇒ **不假装知道**）。
    """
    if not hasattr(ctypes, "windll"):
        return None
    try:
        hwnd = win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        h = parent or hwnd
        got = ctypes.c_int(-1)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            h, 20, ctypes.byref(got), ctypes.sizeof(got))
        return int(got.value)
    except Exception:  # noqa: BLE001
        return None


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

    ★ 2026-10-15 起同一个钩子还负责 `allow_maximize(w)` ——
      **所有现有与将来的对话框**自动拿回标题栏最大化按钮（一处修，全部生效）✓
    """
    key = str(root)
    if key in _installed:
        return False
    _installed.add(key)

    def _on_map(event):
        w = event.widget
        if not isinstance(w, tk.Toplevel) or w is root:
            return                      # 子控件事件、以及主窗，一律跳过
        # ★★ [界面-10]／[界面-09] **根因修**（2026-09-20 用户亲令）：
        #   **无边框/浮层窗口一律不施加** —— 这类窗口**没有标题栏**，下面三件事对它全是错的：
        #     · `fit_window`          ＝ 浮层尺寸应由内容定（检索栏候选浮层被撑开）
        #     · `allow_maximize`      ＝ 给它加 `WS_MAXIMIZEBOX`（浮层哪来的最大化按钮）
        #     · `apply_dark_title_bar`＝ 给**本来就无边框**的窗口施加 DWM 标题栏属性
        #   ★ 坐实「[界面-10] 由 `[界面-05]` 引入」（**三方一致**，⛔ 非推测）：
        #     ① 现盘本段的自报归属注释 `# ★ [界面-05 2026-09-19] …同一处修、所有对话框生效`（见下）；
        #     ② `version.py` L17-18 记 v1.12.7 因 `[界面-05]` 把系统标题栏改暗色；
        #     ③ ★ **更早快照**（`测试\_gate_tmp\_mut_base\ui_fit.py` ＝ 23,814 B／`633581842475FBBF`
        #        ／mtime **09-15 22:28:41**（**早于 09-19**））里 **`def apply_dark_title_bar` 命中 0**
        #        ⇒ 该整套当时不存在。
        #   ⛔ 不动 `apply_dark_title_bar` 本体：**本体无过，错在施加面过宽**。
        if w.overrideredirect():
            return
        if not w.winfo_ismapped():
            return
        # ★★ [界面-11] **只补一次**（2026-09-20 用户亲令「先设好，再显示」）：
        #   下面三件事属 **Win32 层**，平台要求**必须**在窗口真正显示之后做
        #   （① `allow_maximize`：Tk 在 `transient()`/`deiconify()` 时重设样式，提前设会被覆盖；
        #     ② `apply_dark_title_bar`：DWM 沉浸式暗色**只在窗口真显示后**生效）。
        #   ⇒ 既不能前移，就**收成一处、只做一次**：原先"每 Map 都补"让同一件窗口被反复施加
        #     （＝`[界面-08]`"打地鼠"体感的结构原因之一），现在打过标记就跳过 ✓
        if getattr(w, "_bamod_win32_done", False):
            return
        w._bamod_win32_done = True
        try:
            fit_window(w, cap=cap, set_minsize=False)
        except Exception:  # noqa: BLE001
            pass
        # ★ 最大化按钮：`transient()` 会把 WS_MAXIMIZEBOX 抹掉 ⇒ **必须**在映射之后补一次
        #   （见 `allow_maximize` 的纪律：提前设会被 Tk 覆盖）—— ⛔ 不再"每 Map 都补" ✓
        try:
            allow_maximize(w)
        except Exception:  # noqa: BLE001
            pass
        # ★ [界面-05 2026-09-19] 标题栏暗色：**同一处修、所有对话框生效**
        #   （DWM 只在窗口真正显示后才生效 ⇒ 同样**必须**在 map 之后；⛔ 不再"每 Map 都补" ✓）
        try:
            apply_dark_title_bar(w, True)
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


# ══════════════════════════════════════════════════════════════════════════════
# 折叠栏（2026-10-15 用户要求："我的 bundle 的信息太多了，做些折叠栏吧"）
# ══════════════════════════════════════════════════════════════════════════════
class Collapsible:
    r"""**可折叠分区**：一行标题（▶/▼ + 名字 + 可选摘要），点标题展开/收起内容。

    为什么要有它：对话框越加越长，一次把十来个分区全铺开会**淹没重点**
    （用户原话："信息太多了"）；而 tkinter/ttk **没有**内置折叠控件 ⇒ 自己做一个。

    ⛔ 实现只用 `ttk`（不用 `tk.Frame`/`tk.Label`）：
      ttk 控件跟随全局 `Style`，经典 tk 控件**不跟随**（历史上就是它造成过"刺眼白底"✗）⇒
      用 ttk 就天然继承深色主题，不必再走 `paint_classic()` 兜底 ✓

    用法::

        sec = Collapsible(parent, "③ 新建 / 加资产", expanded=False)
        sec.pack(fill="x", pady=(8, 0))
        ttk.Label(sec.body, text="…").grid(row=0, column=0)   # ← 内容放进 sec.body

    * `sec.body`    ：内容容器（**和普通 Frame 一样用**，grid/pack 随便）
    * `sec.expanded`：当前状态；`sec.toggle() / expand() / collapse()`
    * `sec.summary`  ：标题右侧的一句话摘要（如 "9 条"、"3 个 bundle"）——折叠时也看得见 ✓
    折叠**不销毁**内容：控件的变量/回调都还在，「全部展开」一按就回来 ✓
    """

    def __init__(self, parent, title, expanded=False, summary="", ttk=None, padx=8, pady=6,
                 on_toggle=None):
        import tkinter.ttk as _ttk
        self._ttk = ttk or _ttk
        self._on_toggle = on_toggle
        self.frame = self._ttk.Frame(parent)
        self._title = title
        self._expanded = bool(expanded)
        # 标题行：整行可点（用 Button ⇒ 天然可 Tab 聚焦、空格/回车也能触发，比裸 Label 好）
        self.header = self._ttk.Frame(self.frame)
        self.header.pack(fill="x")
        self._btn = self._ttk.Button(self.header, text=self._header_text(summary),
                                     command=self.toggle, takefocus=True)
        self._btn.pack(side="left", fill="x", expand=True)
        # 标题右侧的小按钮（可选，调用方自己放）
        self.extra = self._ttk.Frame(self.header)
        self.extra.pack(side="right")
        self.body = self._ttk.Frame(self.frame, padding=(padx, pady))
        self._summary = summary
        if self._expanded:
            self.body.pack(fill="x")

    # ---- 标题文字 ----
    def _header_text(self, summary=None):
        s = self._summary if summary is None else summary
        arrow = "▼" if self._expanded else "▶"
        return "%s  %s%s" % (arrow, self._title, ("    —— " + s) if s else "")

    def _refresh_title(self):
        try:
            self._btn.config(text=self._header_text())
        except Exception:  # noqa: BLE001
            pass

    # ---- 状态 ----
    @property
    def expanded(self):
        return self._expanded

    @property
    def summary(self):
        return self._summary

    @summary.setter
    def summary(self, text):
        self._summary = text or ""
        self._refresh_title()

    def toggle(self):
        self.collapse() if self._expanded else self.expand()
        return self._expanded

    def _notify(self):
        """状态变了 ⇒ 告诉调用方（对话框用它把窗口重新长到够用）。

        ⛔ 为什么必须回调：`install_autofit` 的 `<Map>` 钩子**只在窗口第一次显示时**跑一次；
          之后用户点标题展开一块，内容变高 ⇒ 不重新 fit 就会被窗口底部截掉 ✗（实测）
        """
        if self._on_toggle:
            try:
                self._on_toggle(self)
            except Exception:  # noqa: BLE001 - 回调失败不影响折叠本身
                pass

    def expand(self):
        if not self._expanded:
            self._expanded = True
            self.body.pack(fill="x")
            self._refresh_title()
            self._notify()
        return self._expanded

    def collapse(self):
        if self._expanded:
            self._expanded = False
            self.body.pack_forget()
            self._refresh_title()
            self._notify()
        return self._expanded

    # ---- 给"全部展开/折叠"用 ----
    def pack(self, **kw):
        self.frame.pack(**kw)
        return self

    def grid(self, **kw):
        self.frame.grid(**kw)
        return self

    def columnconfigure(self, *a, **kw):
        return self.body.columnconfigure(*a, **kw)

    def rowconfigure(self, *a, **kw):
        return self.body.rowconfigure(*a, **kw)


def expand_all(sections, expand=True):
    r"""一次展开/收起一组折叠栏（对话框顶部的「全部展开/折叠」按钮用）。→ 新状态"""
    for s in sections:
        try:
            s.expand() if expand else s.collapse()
        except Exception:  # noqa: BLE001
            pass
    return bool(expand)


# ====================================================================== ttk 全局兜底

#: ★ `[界面-08]` 一次修净 —— **ttk 样式兜底表**（style 名 → {选项: **palette 键名**}）。
#: ⛔ **这里不写任何色值**：值一律从传入的 palette **按名取**（缺键则回退 `DARK_FALLBACK`）
#:   ⇒ 满足「⛔ 不新写死色值、⛔ 不留两套板子（两腿色值同一来源）」✓
#: ★ 为什么要有这张表：产品里九个件只有 `ba_db_tool.py` 有 style 配置，其余八件的 ≈247 处
#:   ttk 实例在该件内**零 style**（`my_bundle.py` 77 处、`mod_assets.py` 65 处…）⇒
#:   **每加一个未配 style 的 ttk 控件就冒一次白**（用户原话「反反复复出现」）✓
#:   ⇒ 修法 ＝ **全局兜底一处配全**（⛔ 不挨个改 247 个控件 ✗）
_TTK_SPECS = {
    ".": {"background": "bg", "foreground": "fg", "fieldbackground": "entry_bg",
          "bordercolor": "border"},
    "TFrame": {"background": "bg"},
    "TLabel": {"background": "bg", "foreground": "fg"},
    "TButton": {"background": "btn_bg", "foreground": "btn_fg",
                "lightcolor": "btn_bg", "darkcolor": "btn_bg", "bordercolor": "border"},
    "TCheckbutton": {"background": "bg", "foreground": "fg",
                     "indicatorbackground": "entry_bg", "indicatorforeground": "fg",
                     "bordercolor": "border"},
    # ★ `TRadiobutton` ＝ **用户截图那行单选按钮**（`my_bundle.py` L1924／L1926 两处）——
    #   原先**整张表里没有它** ⇒ 走默认 ttk 主题 ⇒ 白底 ✗（这就是本条最典型的复发实例）
    "TRadiobutton": {"background": "bg", "foreground": "fg",
                     "indicatorbackground": "entry_bg", "indicatorforeground": "fg",
                     "bordercolor": "border"},
    "TMenubutton": {"background": "btn_bg", "foreground": "btn_fg",
                    "arrowcolor": "btn_fg", "bordercolor": "border"},
    "TSpinbox": {"fieldbackground": "entry_bg", "foreground": "entry_fg",
                 "arrowcolor": "btn_fg", "background": "btn_bg"},
    "TLabelframe": {"background": "bg", "foreground": "fg", "bordercolor": "border"},
    "TLabelframe.Label": {"background": "bg", "foreground": "fg"},
    "TPanedwindow": {"background": "bg"},
    "TSeparator": {"background": "border"},
    "TEntry": {"fieldbackground": "entry_bg", "foreground": "entry_fg",
               "insertcolor": "entry_fg"},
    "TCombobox": {"fieldbackground": "entry_bg", "background": "btn_bg",
                  "foreground": "entry_fg", "arrowcolor": "btn_fg",
                  "selectbackground": "sel", "selectforeground": "sel_fg"},
    "Treeview": {"background": "tree_bg", "fieldbackground": "tree_bg",
                 "foreground": "tree_fg"},
    "Treeview.Heading": {"background": "panel", "foreground": "label_key", "relief": "flat"},
    "TNotebook": {"background": "bg", "borderwidth": 0, "tabmargins": (0, 4, 0, 0)},
    "TNotebook.Tab": {"background": "btn_bg", "foreground": "btn_fg",
                      "padding": (16, 8), "borderwidth": 0},
    "TScrollbar": {"background": "btn_bg", "troughcolor": "panel",
                   "bordercolor": "panel", "arrowcolor": "btn_fg"},
    "TProgressbar": {"background": "accent", "troughcolor": "panel"},
}

#: 状态映射（同 `_TTK_SPECS`：色值也给**键名**）
_TTK_MAPS = {
    "TButton": {"background": [("active", "btn_active"), ("pressed", "btn_active")],
                "foreground": [("disabled", "disabled")]},
    "TCheckbutton": {"background": [("active", "bg"), ("pressed", "bg")],
                     "foreground": [("disabled", "disabled")]},
    "TRadiobutton": {"background": [("active", "bg"), ("pressed", "bg")],
                     "foreground": [("disabled", "disabled")]},
    "TCombobox": {"fieldbackground": [("readonly", "btn_bg")],
                  "foreground": [("readonly", "btn_fg")],
                  "selectbackground": [("readonly", "btn_bg")],
                  "selectforeground": [("readonly", "btn_fg")]},
    "Treeview": {"background": [("selected", "sel")],
                 "foreground": [("selected", "sel_fg")]},
    "Treeview.Heading": {"background": [("active", "btn_active")]},
    "TNotebook.Tab": {"background": [("selected", "tree_bg"), ("active", "btn_active")],
                      "foreground": [("selected", "fg")]},
    "TScrollbar": {"background": [("active", "btn_active")]},
}


def _pal(win, palette, key, default=None):
    """从 palette 或 `DARK_FALLBACK` 按**名**取一个值（⛔ 表里不写死色值）。"""
    for src in (palette, DARK_FALLBACK):
        if isinstance(src, dict) and key in src:
            return src[key]
    return default


def apply_ttk_dark(root, palette=None, extra=None):
    r"""★ `[界面-08]` **全局兜底**：把 ttk 默认样式一次配成暗色（含新加的未配控件）。

    * `palette` ＝ 主程序调色板（`app.c`）；⛔ 缺省不用"另一套板子"，直接用 `DARK_FALLBACK` ✓
    * `extra` ＝ 需要**额外参数**的样式（例：`{"Treeview": {"rowheight": 30, "font": (…, 11)}}`）
      ⇒ 用于 `ba_db_tool` 那种"基础色由兜底给、行高字体各窗自定"的场合（⛔ 不重复两份表）✓
    * 返回 `(oki, 失败样式名 [..])`：⛔ 不吞异常 —— 配不上的样式**点名**带出来（供判据用）✓
    """
    try:
        import tkinter.ttk as ttk
    except Exception as e:                                            # noqa: BLE001
        return (False, ["ttk 不可用：%r" % (e,)])
    ok = True
    bad = []
    try:
        style = ttk.Style(root)
        try:
            style.theme_use("clam")        # clam 才允许把每个部件都上色
        except Exception:                                             # noqa: BLE001
            pass
        for name, spec in _TTK_SPECS.items():
            opts = {}
            for opt, pkey in spec.items():
                v = _pal(root, palette, pkey) if isinstance(pkey, str) else pkey
                if v is not None:
                    opts[opt] = v
            ex = (extra or {}).get(name)
            if isinstance(ex, dict):
                opts.update(ex)
            try:
                style.configure(name, **opts)
            except Exception as e:                                    # noqa: BLE001
                ok = False
                bad.append("%s（%r）" % (name, e))
        for name, spec in _TTK_MAPS.items():
            m = {}
            for opt, pairs in spec.items():
                out = []
                for state, pkey in pairs:
                    v = _pal(root, palette, pkey) if isinstance(pkey, str) else pkey
                    if v is not None:
                        out.append((state, v))
                if out:
                    m[opt] = out
            try:
                style.map(name, **m)
            except Exception as e:                                    # noqa: BLE001
                ok = False
                bad.append("map %s（%r）" % (name, e))
    except Exception as e:                                            # noqa: BLE001
        ok = False
        bad.append("Style() 起不来：%r" % (e,))
    return (ok, bad)


