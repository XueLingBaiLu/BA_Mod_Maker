# -*- coding: utf-8 -*-
r"""音频对话框的共用 UI 工具。

⛔ 为什么需要：`transient(master)` **不保证**新窗口出现在主窗之上 —— 实测两个音频
对话框默认落在 `+0+0`，正好被主窗完全盖住 ⇒ 用户点菜单"看不到任何反应" ✗。
这里统一「居中到主窗 + 置顶 + 抢焦点 + 模态」，并且把"没反应"这件事从根上消掉。
"""


def fit_and_center(dlg, master, min_w=800, min_h=520, extra_h=0):
    r"""按内容自适应窗口大小后居中到主窗，**保证底部控件始终在可见区内**。

    ⛔ 为什么需要：写死 `geometry("860x560")` 在高 DPI / 大字体（Windows 缩放
    150%~200%）下小于内容所需高度，而 Tk 的 pack 按放入顺序分配空间 —— 后放的
    按钮行会被裁到窗口外，用户必须手动**拉伸窗口**才看得到按钮。
    这里用 `winfo_reqheight()`（内容实际需要的像素高度）反推窗口尺寸，并把它
    设为 minsize，使窗口无法被缩到裁掉按钮的程度。
    """
    try:
        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        w = min(max(min_w, dlg.winfo_reqwidth() + 4), int(sw * 0.92))
        h = min(max(min_h, dlg.winfo_reqheight() + 4 + int(extra_h)), int(sh * 0.92))
        dlg.geometry("%dx%d" % (w, h))
        dlg.minsize(w, h)
    except Exception:  # noqa: BLE001 - 尺寸自适应失败不影响功能
        pass
    # ★ 2026-10-15：`transient()` 会把标题栏的**最大化按钮**抹掉（Win32 清掉 WS_MAXIMIZEBOX）
    #   ⇒ 这里补回来（本模块是音频对话框的统一收尾点，一处修、两个对话框都生效）✓
    try:
        import ui_fit
        ui_fit.allow_maximize(dlg)
    except Exception:  # noqa: BLE001
        pass
    return center_on_parent(dlg, master)


def theme_from_master(dlg, master):
    r"""让对话框继承主程序配色（**保留此处，实现已搬到 `ui_fit`，两处共用一份**）。

    ⛔ 为什么需要：主程序是深色主题（bg #000000 / entry_bg #0c111c），而
    `tk.Text`、`tk.Listbox` 这类**经典控件不跟随 ttk 主题**，默认是刺眼的白色底
    （用户实测反馈：文件栏白底很刺眼；同类问题在「我的 bundle」的 ② / ④ 显示栏又出现一次）。
    主窗已有 `theme_children()` 统一上色，这里递归调用一次即可，**不要在对话框里硬编码颜色**。

    ⛔ 关键坑：生产代码里对话框的 master 是 **`Tk` 根窗口**（`AudioAddDialog(self.root)`），
    根窗口**没有** `theme_children` ⇒ 直接取 `master.theme_children` 会静默失败、白底依旧。
    因此改为从 Tk 根窗口取回 app 引用（`EditorApp.__init__` 里挂了 `root._ba_app`）。

    ★ v1.8.112：实现搬到 `ui_fit.theme_from_master`，并加了"经典控件兜底上色"这一级
    （拿不到 app 时也不再有白底）⇒ 本函数只是**转发**，老调用方不受影响 ✓
    """
    import ui_fit
    return ui_fit.theme_from_master(dlg, master)


def center_on_parent(dlg, master):
    """把对话框居中到主窗、提到最前、设为模态。"""
    try:
        dlg.update_idletasks()
        mw = master.winfo_width() or 1200
        mh = master.winfo_height() or 800
        mx = master.winfo_rootx()
        my = master.winfo_rooty()
        w = dlg.winfo_width() or 800
        h = dlg.winfo_height() or 560
        x = mx + max((mw - w) // 2, 0)
        y = my + max((mh - h) // 4, 0)
        dlg.geometry("+%d+%d" % (x, y))
    except Exception:  # noqa: BLE001 - 定位失败不影响功能
        pass
    try:
        dlg.lift()
        dlg.attributes("-topmost", True)
        dlg.after(400, lambda: dlg.attributes("-topmost", False))
        dlg.focus_force()
        dlg.grab_set()          # 模态：主窗点不动，杜绝"看起来没反应"
    except Exception:  # noqa: BLE001
        pass
