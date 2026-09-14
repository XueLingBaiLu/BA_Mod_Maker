# -*- coding: utf-8 -*-
r"""音频导入（FMOD 音库文件级）：列出游戏音库、备份/替换/还原、批量替换。

断箭音频在 `BrokenArrow_Data\StreamingAssets` 的 FMOD Studio 音库（*.bank）与
流式音频（*.gts / *.gtp）里；bundle 内无 AudioClip，不走 .bamod 管线。
StreamingAssets 无 CRC 校验 → 整文件替换即可生效。

样本级编辑（提取/重混单个音效）需要解析 FMOD Studio 银行格式（LIST 元数据 +
SND 样本流，无 FSB5 标记），属单独逆向项目；本功能做文件级导入 + 安全备份还原。
"""
import os
import glob
import shutil
import tkinter as tk
import audio_ui
from tkinter import ttk, filedialog, messagebox

BANK_EXTS = (".bank", ".gts", ".gtp")
BACKUP_DIRNAME = "_ba_audio_backup"


def detect_streaming_assets():
    """定位游戏 StreamingAssets 目录（返回路径或 None）。"""
    import ba_db_tool as _app  # noqa: F401  仅供文档说明，实际由调用方传入
    roots = [
        "D:/Steam/steamapps/common",
        "C:/Program Files (x86)/Steam/steamapps/common",
        "C:/Program Files/Steam/steamapps/common",
        "E:/Steam/steamapps/common",
        "F:/Steam/steamapps/common",
    ]
    for root in roots:
        for g in glob.glob(os.path.join(root, "*")):
            base = os.path.basename(g).lower()
            if not (base.startswith("broken") or base.startswith("arrow")):
                continue
            sa = os.path.join(g, "BrokenArrow_Data", "StreamingAssets")
            if os.path.isdir(sa):
                return sa
    return None


def list_audio_files(sa):
    """[(文件名, 大小, 修改时间)]，按名字排序。"""
    out = []
    for fn in sorted(os.listdir(sa)):
        p = os.path.join(sa, fn)
        if not os.path.isfile(p) or not fn.lower().endswith(BANK_EXTS):
            continue
        st = os.stat(p)
        out.append((fn, st.st_size, st.st_mtime))
    return out


def backup_path(sa, fn):
    return os.path.join(sa, BACKUP_DIRNAME, fn)


def is_backed_up(sa, fn):
    return os.path.isfile(backup_path(sa, fn))


def backup_audio(sa, fn):
    """备份原文件（已备份则不重复）。"""
    src = os.path.join(sa, fn)
    dst = backup_path(sa, fn)
    if os.path.isfile(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def restore_audio(sa, fn):
    """从备份还原。"""
    dst = backup_path(sa, fn)
    if not os.path.isfile(dst):
        raise FileNotFoundError("没有备份：" + fn)
    shutil.copy2(dst, os.path.join(sa, fn))
    return True


def replace_audio(sa, fn, src_file):
    """用新文件覆盖音库（先自动备份原文件）。"""
    if os.path.normcase(os.path.abspath(src_file)) == os.path.normcase(
            os.path.abspath(os.path.join(sa, fn))):
        raise ValueError("不能选同一个文件")
    backup_audio(sa, fn)
    shutil.copy2(src_file, os.path.join(sa, fn))
    return True


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


class AudioImportDialog(tk.Toplevel):
    """音频导入对话框：音库列表 + 替换/还原/批量。"""

    def __init__(self, master, sa=None):
        super().__init__(master)
        self.title("导入音频/音效（FMOD 音库）")
        self.transient(master)
        self.sa = sa or detect_streaming_assets()
        self.rows = []  # [fn, size, mtime]

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(side="top", fill="x")
        self.sa_lbl = ttk.Label(top, text="音库目录：%s" % (self.sa or "未检测到"),
                                foreground="#555", wraplength=700, justify="left")
        self.sa_lbl.pack(side="left")
        ttk.Button(top, text="刷新", command=self._refresh).pack(side="right")

        # 底部（提示 + 按钮行）**先占位**：Tk 的 pack 按放入顺序分配空间，若先放
        # 展开的 Treeview，高 DPI / 大字体下它会吃掉整窗高度，把按钮行挤出可见区
        # ⇒ 用户必须手动拉伸窗口才能看到按钮。
        hint = ttk.Label(frm, foreground="#777", justify="left", wraplength=820,
                         text="提示：断箭音效在 FMOD 音库（*.bank + .gts/.gtp）里，替换整文件即可生效"
                              "（StreamingAssets 无 CRC 校验）；首次替换自动备份到 "
                              "<StreamingAssets>\\%s\\。" % BACKUP_DIRNAME)
        hint.pack(side="bottom", fill="x", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(btns, text="添加新音效…", command=self._open_add).pack(side="left", padx=2)
        ttk.Button(btns, text="替换选中…", command=self._replace_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="批量替换（按文件名）…", command=self._batch_replace).pack(side="left", padx=2)
        ttk.Button(btns, text="还原选中", command=self._restore_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="备份全部", command=self._backup_all).pack(side="left", padx=2)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")

        mid = ttk.Frame(frm)
        mid.pack(side="top", fill="both", expand=True, pady=(8, 0))
        cols = ("name", "size", "time", "state")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="音库文件")
        self.tree.heading("size", text="大小")
        self.tree.heading("time", text="修改时间")
        self.tree.heading("state", text="备份状态")
        self.tree.column("name", width=360, minwidth=200)
        self.tree.column("size", width=100, stretch=False)
        self.tree.column("time", width=150, stretch=False)
        self.tree.column("state", width=100, stretch=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self._refresh()
        audio_ui.theme_from_master(self, master)
        audio_ui.fit_and_center(self, master, 860, 520)

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.sa or not os.path.isdir(self.sa):
            self.sa_lbl.config(text="音库目录：未检测到（请手动放到游戏 StreamingAssets 下）")
            return
        self.sa_lbl.config(text="音库目录：%s" % self.sa)
        import time as _time
        self.rows = list_audio_files(self.sa)
        for fn, size, mtime in self.rows:
            state = "已备份" if is_backed_up(self.sa, fn) else "未备份"
            self.tree.insert("", "end", values=(
                fn, _fmt_size(size), _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime)), state))

    def _open_add(self):
        """打开「添加新音效」对话框（继承当前检测到的音库目录）。"""
        try:
            import audio_add
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))
            return
        audio_add.AudioAddDialog(self, sa=self.sa)

    def _selected_fn(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一个音库文件")
            return None
        vals = self.tree.item(sel[0], "values")
        return vals[0]

    def _need_sa(self):
        """音库目录没检测到就**明确报错**。

        ⛔ 旧写法是 `if not self.sa: return` —— **静默返回** ⇒ 用户点按钮毫无反应，
        这正是"点了没反应"的来源之一 ✗（对话框能开、列表空、按钮全是哑的）。
        """
        if not self.sa or not os.path.isdir(self.sa):
            messagebox.showerror(
                "未找到音库目录",
                "没有检测到游戏的 StreamingAssets 目录，下面的列表是空的，"
                "所以任何操作都不会生效。\n\n"
                "解决：先用【文件 → 打开 data.unity3d】打开一次数据库"
                "（工具会记住游戏目录），再回来打开本窗口；\n"
                "或确认游戏装在 Steam 的 common 目录下。")
            return False
        return True

    def _replace_selected(self):
        if not self._need_sa():
            return
        fn = self._selected_fn()
        if not fn:
            return
        src = filedialog.askopenfilename(
            title="选择新音频文件（替换 %s）" % fn,
            filetypes=[("音库/音频", "*.bank *.gts *.gtp *.wav *.ogg"), ("全部", "*.*")])
        if not src:
            return
        try:
            was_backup = backup_audio(self.sa, fn)
            replace_audio(self.sa, fn, src)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("替换失败", str(e))
            return
        self._refresh()
        messagebox.showinfo("完成", "已替换 %s%s。进游戏即可听到新音效。" % (
            fn, "（原文件已备份）" if was_backup else ""))

    def _batch_replace(self):
        if not self._need_sa():
            return
        d = filedialog.askdirectory(title="选择包含新音频文件的文件夹（按文件名匹配覆盖）")
        if not d:
            return
        done, skipped = 0, 0
        for fn in os.listdir(d):
            if not fn.lower().endswith(BANK_EXTS):
                continue
            if not os.path.isfile(os.path.join(self.sa, fn)):
                skipped += 1
                continue
            try:
                backup_audio(self.sa, fn)
                replace_audio(self.sa, fn, os.path.join(d, fn))
                done += 1
            except Exception as e:  # noqa: BLE001
                print("[音频] 批量替换失败 %s: %s" % (fn, e))
        self._refresh()
        messagebox.showinfo("批量替换完成", "成功 %d 个，跳过（目录无同名）%d 个" % (done, skipped))

    def _restore_selected(self):
        if not self._need_sa():
            return
        fn = self._selected_fn()
        if not fn:
            return
        try:
            restore_audio(self.sa, fn)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("还原失败", str(e))
            return
        self._refresh()
        messagebox.showinfo("完成", "已还原 %s" % fn)

    def _backup_all(self):
        if not self._need_sa():
            return
        n = 0
        for fn, _size, _mt in self.rows:
            try:
                if backup_audio(self.sa, fn):
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        self._refresh()
        messagebox.showinfo("完成", "新备份 %d 个音库文件 → %s" % (n, BACKUP_DIRNAME))


def open_dialog(master):
    win = AudioImportDialog(master)
    return win
