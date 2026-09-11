# -*- coding: utf-8 -*-
"""素材导入（BA_Mod_Maker 菜单功能）：把 .bamod 包合并进游戏 bundle + 计算 CRC；
图片/图标/肖像直接导入（自定义容器路径与映射地址，像导入模型一样）。

打包版 exe 运行时 _rev_tools/_unitypy 在 _internal 下；开发运行时就在工作区根目录。
"""
import os
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

_GAME_ROOTS = [
    "D:/Steam/steamapps/common/broken_arrow",
    "C:/Program Files (x86)/Steam/steamapps/common/broken_arrow",
    "C:/Program Files/Steam/steamapps/common/broken_arrow",
    "E:/Steam/steamapps/common/broken_arrow",
    "F:/Steam/steamapps/common/broken_arrow",
]

# 图片 bundle 类别：类别键 → (显示名, 容器路径前缀, 默认映射地址模板, 参考条目子串)
# 地址模板按游戏实测约定：肖像=国家\目录\文件名（反斜杠）、标签=文件名-Label、武器/弹药图标=文件名本身。
ICON_CATEGORIES = [
    ("unitportraits_assets_all", "单位肖像 (unitportraits)",
     "Assets/Resources_moved/Images/UnitPortraits/RU/", "{RU}\\{name}\\{name}", "Images/UnitPortraits"),
    ("unitlabels_assets_all", "单位标签/缩略图 (unitlabels)",
     "Assets/Resources_moved/Images/Labels/Icons/", "{name}-Label", "Images/Labels"),
    ("unitweaponicons_assets_all", "武器图标 (unitweaponicons)",
     "Assets/Resources_moved/Images/Weapons/Icons/", "{name}", "Images/Weapons"),
    ("ammoicons_assets_all", "弹药图标 (ammoicons)",
     "Assets/Resources_moved/Images/Ammunition/Icons/", "{name}", "Images/Ammunition"),
    ("nations&specs_assets_all", "国家/专精 (nations&specs)",
     "Assets/Resources_moved/Images/Nations/", "{name}", "Images/"),
]
CAT_BY_KEY = {c[0]: c for c in ICON_CATEGORIES}
# 类别中文显示名（下拉框与状态提示用；技术键保留给高级用户）
CAT_DISP = {
    "unitportraits_assets_all": "单位肖像",
    "unitlabels_assets_all": "单位标签/缩略图",
    "unitweaponicons_assets_all": "武器图标",
    "ammoicons_assets_all": "弹药图标",
    "nations&specs_assets_all": "国家/专精",
}
CAT_DISP_FULL = {k: "%s（%s）" % (v, k) for k, v in CAT_DISP.items()}


def _fit_window(win, w, h, master):
    """按屏幕尺寸自适应窗口：取 屏幕比例与预设值的较小者，居中偏上放置。"""
    try:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
    except Exception:
        sw, sh = 1024, 768
    width = max(680, min(w, int(sw * 0.92)))
    height = max(420, min(h, int(sh * 0.85)))
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    win.geometry("%dx%d+%d+%d" % (width, height, x, y))
    win.minsize(680, 420)
    win.resizable(True, True)
    if master is not None:
        try:
            win.transient(master)
        except Exception:
            pass


def guess_category(path):
    """容器路径 → 图片类别键。"""
    p = (path or "").replace("\\", "/")
    if "UnitPortraits" in p:
        return "unitportraits_assets_all"
    if "/Labels" in p or p.endswith("-Label.png") or p.endswith("-label.png"):
        return "unitlabels_assets_all"
    if "Ammunition" in p:
        return "ammoicons_assets_all"
    if "Weapon" in p:
        return "unitweaponicons_assets_all"
    if "Nations" in p or "Spec" in p or "Specialization" in p:
        return "nations&specs_assets_all"
    return "unitportraits_assets_all"


def default_path_for(cat_key, name):
    """类别 + 文件名(无扩展名) → 默认容器路径。"""
    prefix = CAT_BY_KEY[cat_key][2]
    if cat_key == "unitlabels_assets_all":
        return prefix + name + "-Label.png"
    return prefix + name + "/" + name + ".png" if cat_key == "unitportraits_assets_all" \
        else prefix + name + ".png"


def default_address_for(cat_key, name):
    """类别 + 文件名(无扩展名) → 默认映射地址。"""
    tpl = CAT_BY_KEY[cat_key][3]
    return tpl.replace("{name}", name).replace("{RU}", "RU")


def _setup_paths():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        internal = os.path.join(os.path.dirname(sys.executable), "_internal")
        rev = os.path.join(internal, "_rev_tools")
        unity = os.path.join(internal, "_unitypy")
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        rev = os.path.join(here, "_rev_tools")
        unity = os.path.join(here, "_unitypy")
    for p in (rev, unity):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return rev, unity


def detect_game_bundle():
    """自动检测游戏目录下的 units bundle + catalog + 全部图片 bundle。"""
    import glob
    for d in _GAME_ROOTS:
        pc = os.path.join(d, "BrokenArrow_Data", "StreamingAssets", "aa", "PC")
        if not os.path.isdir(pc):
            continue
        cands = glob.glob(os.path.join(pc, "units_assets_all_*.bundle"))
        if cands:
            catalog = os.path.join(d, "BrokenArrow_Data", "StreamingAssets", "aa", "catalog.json")
            image_bundles = {}
            for key, _d, _p, _t, _r in ICON_CATEGORIES:
                hits = glob.glob(os.path.join(pc, key + "_*.bundle"))
                if hits:
                    image_bundles[key] = hits[0]
            return cands[0], catalog, image_bundles
    return None, None, {}


class TexturePackDialog(tk.Toplevel):
    """把 PNG 图标/肖像打包成 .bamod（每张 = Texture2D + Sprite 两条容器条目）。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("打包图标/肖像 (.bamod)")
        self.geometry("700x330")
        self.transient(master)
        self.grab_set()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="PNG 文件（可多选）：").grid(row=0, column=0, sticky="w")
        self.files_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.files_var, width=56).grid(row=0, column=1, sticky="we")
        ttk.Button(frm, text="浏览…", command=self._browse).grid(row=0, column=2)

        ttk.Label(frm, text="容器路径前缀：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.base_var = tk.StringVar(
            value="Assets/Resources_moved/Images/UnitPortraits/RU/MY_UNIT/")
        ttk.Entry(frm, textvariable=self.base_var, width=56).grid(row=1, column=1, sticky="we", pady=(8, 0))
        ttk.Label(frm, text="（最终路径 = 前缀 + 文件名，如 …/MY_ICON.png）", foreground="#777")\
            .grid(row=2, column=1, sticky="w")

        ttk.Label(frm, text="输出包：").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.out_var = tk.StringVar(value="<工作目录>/MY_ICONS.bamod")
        ttk.Entry(frm, textvariable=self.out_var, width=56).grid(row=3, column=1, sticky="we", pady=(8, 0))
        ttk.Button(frm, text="浏览…", command=self._browse_out).grid(row=3, column=2, pady=(8, 0))

        self.status = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status, foreground="#555").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="开始打包", command=self._run).pack(side="left", padx=4)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left")

        frm.columnconfigure(1, weight=1)

    def _browse(self):
        fs = filedialog.askopenfilenames(title="选择 PNG 图标/肖像", filetypes=[("PNG", "*.png"), ("全部", "*.*")])
        if fs:
            self.files_var.set(";".join(fs))

    def _browse_out(self):
        p = filedialog.asksaveasfilename(title="输出 .bamod", defaultextension=".bamod",
                                         filetypes=[("bamod 包", "*.bamod")])
        if p:
            self.out_var.set(p)

    def _run(self):
        files = [f for f in self.files_var.get().split(";") if f.strip()]
        base = self.base_var.get().strip()
        out = self.out_var.get().strip()
        if not files:
            messagebox.showerror("错误", "请先选择 PNG 文件")
            return
        if not base.endswith("/"):
            base += "/"
        if not out:
            messagebox.showerror("错误", "请填输出路径")
            return
        self.status.set("正在打包…")
        self.update_idletasks()
        try:
            _setup_paths()
            from pack_model import create_texture_pack
            textures = [(base + os.path.basename(f), f) for f in files]
            p, n, size = create_texture_pack(out, textures)
            self.status.set("完成")
            messagebox.showinfo("完成", "已打包 %d 张图标（%d 字节）：\n%s" % (n, size, p))
        except Exception as e:  # noqa: BLE001
            self.status.set("失败")
            messagebox.showerror("失败", "打包失败：%s" % e)


class AssetImportDialog(tk.Toplevel):
    """.bamod 素材包导入对话框（后台线程执行，避免卡 UI）。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("导入 .bamod 素材包")
        self.geometry("680x340")
        self.transient(master)
        self.grab_set()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="素材包 (.bamod)：").grid(row=0, column=0, sticky="w")
        self.pack_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.pack_var, width=56).grid(row=0, column=1, sticky="we")
        ttk.Button(frm, text="浏览…", command=self._browse_pack).grid(row=0, column=2)

        ttk.Label(frm, text="目标 bundle：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.bundle_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.bundle_var, width=56).grid(row=1, column=1, sticky="we", pady=(8, 0))
        ttk.Button(frm, text="浏览…", command=self._browse_bundle).grid(row=1, column=2, pady=(8, 0))

        ttk.Button(frm, text="自动检测游戏目录", command=self._autodetect).grid(row=2, column=1, sticky="w", pady=(4, 0))

        ttk.Label(frm, text="映射地址（可改）：").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.addr_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.addr_var, width=56).grid(row=3, column=1, sticky="we", pady=(8, 0))

        self.crc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="导入后计算 CRC 并更新 catalog", variable=self.crc_var)\
            .grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.status = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status, foreground="#555").grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="开始导入", command=self._run).pack(side="left", padx=4)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left")

        frm.columnconfigure(1, weight=1)
        b, c = detect_game_bundle()
        if b:
            self.bundle_var.set(b)
        self.catalog = c

    def _browse_pack(self):
        p = filedialog.askopenfilename(title="选择 .bamod 素材包", filetypes=[("bamod 包", "*.bamod"), ("全部", "*.*")])
        if p:
            self.pack_var.set(p)
            base = os.path.basename(p)
            if base.lower().endswith(".bamod"):
                base = base[:-6]
            if base and not self.addr_var.get():
                self.addr_var.set("%s/%s" % (base, base))

    def _browse_bundle(self):
        p = filedialog.askopenfilename(title="选择目标 bundle", filetypes=[("bundle", "*.bundle"), ("全部", "*.*")])
        if p:
            self.bundle_var.set(p)

    def _autodetect(self):
        b, c = detect_game_bundle()
        if b:
            self.bundle_var.set(b)
            self.catalog = c
            self.status.set("已检测到游戏 bundle")
        else:
            messagebox.showwarning("未找到", "没找到游戏目录，请手动选 bundle")

    def _run(self):
        pack = self.pack_var.get().strip()
        bundle = self.bundle_var.get().strip()
        if not pack or not os.path.isfile(pack):
            messagebox.showerror("错误", "请先选择 .bamod 素材包")
            return
        if not bundle or not os.path.isfile(bundle):
            messagebox.showerror("错误", "请选择目标 bundle（游戏目录下的 units_assets_all_*.bundle）")
            return
        catalog = self.catalog
        do_crc = self.crc_var.get()
        address = self.addr_var.get().strip() or None
        import queue as _queue
        q = _queue.Queue()
        self.status.set("正在导入…")
        self.update_idletasks()

        def work():
            try:
                _setup_paths()
                import import_pack
                import zipfile as _zipfile
                import json as _json
                is_skin = False
                is_matswap = False
                skin_info = ""
                try:
                    with _zipfile.ZipFile(pack) as z:
                        mf = _json.loads(z.read("manifest.json"))
                    if mf.get("skin"):
                        is_skin = True
                        sk = mf["skin"]
                        skin_info = "皮肤 %d：%d 材质 / %d 贴图%s" % (
                            sk.get("target_id", 0), len(sk.get("mats") or []),
                            len(sk.get("textures") or []),
                            "（新增皮肤槽 %d）" % sk["new_id"] if sk.get("new_id") else "")
                    elif mf.get("matswap"):
                        is_matswap = True
                        ms = mf["matswap"]
                        skin_info = "模型贴图替换：%d 材质 / %d 贴图 / %d 渲染器" % (
                            len(ms.get("mats") or []), len(ms.get("textures") or []),
                            len(ms.get("renderers") or []))
                except Exception:
                    pass
                n, new_root, crc, removed = import_pack.import_pack(
                    pack, bundle, catalog, address=address,
                    progress=lambda m: q.put(("progress", m)), do_crc=do_crc)
                if is_skin or is_matswap:
                    msg = "导入完成：%s（新增对象 %d 个）" % (skin_info, n)
                else:
                    msg = "导入完成：%d 个对象（清理旧对象 %d 个）" % (n, removed)
                if crc is not None:
                    msg += "\nCRC = %s（catalog 已更新）" % crc
                else:
                    msg += "\n（未计算 CRC）"
                if address:
                    msg += "\n映射地址 %s 已注册" % address
                q.put(("done", msg))
            except Exception as e:  # noqa: BLE001
                q.put(("err", "导入失败：%s" % e))

        def poll():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "progress":
                        self.status.set("正在导入… %s" % val)
                    else:
                        self._done(val, err=(kind == "err"))
                        return
            except _queue.Empty:
                pass
            self.after(200, poll)

        threading.Thread(target=work, daemon=True).start()
        self.after(200, poll)

    def _done(self, msg, err=False):
        self.status.set(msg.split("\n")[0])
        (messagebox.showerror if err else messagebox.showinfo)("结果", msg)


class ImageImportDialog(tk.Toplevel):
    """图片/图标/肖像直接导入游戏（像导入模型一样支持自定义映射地址）。

    多选图片 → 每行自定义「容器路径」（bundle 内路径）与「映射地址」（catalog 地址）
    → 按类别自动分组导入对应图片 bundle（肖像/标签/武器图标/弹药图标/国家专精）
    → 自动 CRC + 可选注册地址；也可只打包 .bamod。
    """

    def __init__(self, master):
        super().__init__(master)
        self.title("导入图片/图标/肖像")
        _fit_window(self, 1020, 620, master)
        self.transient(master)
        self.grab_set()

        self.rows = []  # [{"file","path","addr"}]
        self.catalog = None
        self.units_bundle = None
        self.image_bundles = {}

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        # 类别（决定新文件的默认路径/地址模板；显示中文名）
        top = ttk.Frame(frm)
        top.pack(fill="x")
        ttk.Label(top, text="图片类别：").pack(side="left")
        self.cat_var = tk.StringVar(value="unitportraits_assets_all")
        self.cat_combo = ttk.Combobox(top, textvariable=self.cat_var, state="readonly", width=34)
        # 显示中文名 + 技术键；内部值仍是技术键
        self.cat_combo["values"] = [CAT_DISP_FULL.get(k, k) for k, *_ in ICON_CATEGORIES]
        self.cat_combo.pack(side="left", padx=4)
        self.cat_combo.set(CAT_DISP_FULL["unitportraits_assets_all"])
        self.cat_combo.bind("<<ComboboxSelected>>", self._on_cat_selected)
        ttk.Button(top, text="自动检测游戏目录", command=self._autodetect).pack(side="left", padx=8)
        self.bundle_lbl = tk.StringVar(value="目标 bundle：未检测（导入时按类别自动解析）")
        ttk.Label(top, textvariable=self.bundle_lbl, foreground="#555", wraplength=520,
                  justify="left").pack(side="left", padx=8, fill="x", expand=True)

        # 行列表
        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("file", "path", "addr")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10, selectmode="extended")
        self.tree.heading("file", text="图片文件")
        self.tree.heading("path", text="容器路径（bundle 内，可编辑）")
        self.tree.heading("addr", text="映射地址（catalog，可编辑）")
        self.tree.column("file", width=230, minwidth=140, stretch=False)
        self.tree.column("path", width=380, minwidth=220, stretch=True)
        self.tree.column("addr", width=250, minwidth=160, stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="添加图片…", command=self._add_files).pack(side="left", padx=2)
        ttk.Button(btns, text="移除选中", command=self._remove_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="编辑选中（路径/地址）…", command=self._edit_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="自动生成路径与地址", command=self._autogen).pack(side="left", padx=2)
        ttk.Label(btns, text="（双击行 = 编辑）", foreground="#777").pack(side="left", padx=6)

        # 选项（窗口窄时自动换行）
        opts = ttk.Frame(frm)
        opts.pack(fill="x", pady=(8, 0))
        self.reg_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="注册映射地址（像导入模型一样写进 catalog）", variable=self.reg_var)\
            .pack(side="left")
        self.crc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="计算 CRC 并更新 catalog", variable=self.crc_var).pack(side="left", padx=10)
        self.packonly_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="只打包 .bamod（不导入）", variable=self.packonly_var).pack(side="left")

        outrow = ttk.Frame(frm)
        outrow.pack(fill="x", pady=(4, 0))
        ttk.Label(outrow, text="输出 .bamod（只打包模式用）：").pack(side="left")
        self.out_var = tk.StringVar(value="<工作目录>/MY_ICONS.bamod")
        ttk.Entry(outrow, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(outrow, text="浏览…", command=self._browse_out).pack(side="left")

        # 状态与执行
        self.status = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status, foreground="#555").pack(fill="x", pady=(8, 0))
        runrow = ttk.Frame(frm)
        runrow.pack(fill="x", pady=(6, 0))
        ttk.Button(runrow, text="开始导入", command=self._run).pack(side="right", padx=4)
        ttk.Button(runrow, text="关闭", command=self.destroy).pack(side="right")

        b, c, ib = detect_game_bundle()
        if b:
            self.units_bundle = b
            self.catalog = c
            self.image_bundles = ib
            self._update_bundle_label()

    # ---------- UI helpers ----------
    def _on_cat_selected(self, _event=None):
        """下拉框显示名 → 技术键。"""
        disp = self.cat_combo.get()
        for key, d in CAT_DISP_FULL.items():
            if disp == d:
                self.cat_var.set(key)
                break
        self._update_bundle_label()

    def _update_bundle_label(self):
        if not self.image_bundles:
            self.bundle_lbl.set("目标 bundle：未检测（导入时按类别自动解析）")
            return
        key = self.cat_var.get()
        p = self.image_bundles.get(key)
        disp = CAT_DISP.get(key, key)
        if p:
            self.bundle_lbl.set("目标 bundle（%s）：%s" % (disp, os.path.basename(p)))
        else:
            self.bundle_lbl.set("目标 bundle：已检测 %d 类图片 bundle（导入时按类别自动分组）"
                                % len(self.image_bundles))

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i),
                             values=(r["file"], r["path"], r["addr"]))

    def _selected(self):
        return [int(i) for i in self.tree.selection()]

    def _add_files(self):
        fs = filedialog.askopenfilenames(title="选择图片（PNG 等，可多选）",
                                         filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"),
                                                    ("PNG", "*.png"), ("全部", "*.*")])
        if not fs:
            return
        key = self.cat_var.get()
        added = 0
        for f in fs:
            name = os.path.splitext(os.path.basename(f))[0]
            self.rows.append({"file": f,
                              "path": default_path_for(key, name),
                              "addr": default_address_for(key, name)})
            added += 1
        self._refresh_tree()
        self.status.set("已添加 %d 张图片" % added)

    def _remove_selected(self):
        idxs = sorted(self._selected(), reverse=True)
        for i in idxs:
            if 0 <= i < len(self.rows):
                self.rows.pop(i)
        self._refresh_tree()

    def _edit_selected(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一行")
            return
        row = self.rows[sel[0]]
        dlg = tk.Toplevel(self)
        dlg.title("编辑容器路径与映射地址")
        _fit_window(dlg, 880, 240, self)
        dlg.transient(self)
        dlg.grab_set()
        box = ttk.Frame(dlg, padding=10)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="容器路径（bundle 内，如 …/Images/UnitPortraits/RU/MY_UNIT/MY_ICON.png）：",
                  wraplength=840, justify="left").grid(row=0, column=0, sticky="w")
        path_var = tk.StringVar(value=row["path"])
        ttk.Entry(box, textvariable=path_var).grid(row=1, column=0, sticky="we", pady=(2, 8))
        ttk.Label(box, text="映射地址（catalog 地址，如 RU\\MY_UNIT\\MY_ICON）：",
                  wraplength=840, justify="left").grid(row=2, column=0, sticky="w")
        addr_var = tk.StringVar(value=row["addr"])
        ttk.Entry(box, textvariable=addr_var).grid(row=3, column=0, sticky="we", pady=(2, 8))
        ttk.Label(box, text="图片：%s" % row["file"], foreground="#777",
                  wraplength=840, justify="left").grid(row=4, column=0, sticky="w")

        def apply_edit():
            for i in sel:
                if 0 <= i < len(self.rows):
                    self.rows[i]["path"] = path_var.get().strip()
                    self.rows[i]["addr"] = addr_var.get().strip()
            self._refresh_tree()
            dlg.destroy()

        rowbtn = ttk.Frame(box)
        rowbtn.grid(row=5, column=0, sticky="e")
        ttk.Button(rowbtn, text="应用", command=apply_edit).pack(side="left", padx=4)
        ttk.Button(rowbtn, text="取消", command=dlg.destroy).pack(side="left")
        box.columnconfigure(0, weight=1)

    def _autogen(self):
        sel = self._selected() or list(range(len(self.rows)))
        for i in sel:
            if 0 <= i < len(self.rows):
                r = self.rows[i]
                name = os.path.splitext(os.path.basename(r["file"]))[0]
                key = guess_category(r["path"])
                r["path"] = default_path_for(key, name)
                r["addr"] = default_address_for(key, name)
        self._refresh_tree()
        self.status.set("已按文件名重新生成路径与地址")

    def _browse_out(self):
        p = filedialog.asksaveasfilename(title="输出 .bamod", defaultextension=".bamod",
                                         filetypes=[("bamod 包", "*.bamod")])
        if p:
            self.out_var.set(p)

    def _autodetect(self):
        b, c, ib = detect_game_bundle()
        if b:
            self.units_bundle = b
            self.catalog = c
            self.image_bundles = ib
            self._update_bundle_label()
            self.status.set("已检测到游戏目录（units bundle + %d 类图片 bundle）" % len(ib))
        else:
            messagebox.showwarning("未找到", "没找到游戏目录，请检查 Steam 安装路径")

    # ---------- 执行 ----------
    def _run(self):
        if not self.rows:
            messagebox.showerror("错误", "请先添加图片")
            return
        do_crc = self.crc_var.get()
        do_reg = self.reg_var.get()
        pack_only = self.packonly_var.get()
        out = self.out_var.get().strip() if pack_only else None
        if pack_only and not out:
            messagebox.showerror("错误", "只打包模式需要填输出 .bamod 路径")
            return
        import queue as _queue
        q = _queue.Queue()
        rows = [dict(r) for r in self.rows]
        catalog = self.catalog
        units_bundle = self.units_bundle
        image_bundles = dict(self.image_bundles)
        self.status.set("正在导入…")
        self.update_idletasks()

        def work():
            try:
                _setup_paths()
                from pack_model import create_texture_pack
                import import_pack
                if pack_only:
                    key = self.cat_var.get()
                    textures = [(r["path"], r["file"], r["addr"]) if do_reg else (r["path"], r["file"])
                                for r in rows]
                    p, n, size = create_texture_pack(out, textures, bundle_kind=key)
                    q.put(("done", "已打包 %d 张图片（%d 字节）：\n%s" % (n, size, p)))
                    return
                # 按类别分组（每组对应一个图片 bundle）
                groups = {}
                for r in rows:
                    key = guess_category(r["path"])
                    groups.setdefault(key, []).append(r)
                msgs = []
                total = 0
                for key, items in groups.items():
                    disp = CAT_DISP.get(key, key)
                    bundle = image_bundles.get(key) or units_bundle
                    if not bundle or not os.path.isfile(bundle):
                        raise RuntimeError("找不到%s的图片 bundle（请先自动检测游戏目录）" % disp)
                    with tempfile.NamedTemporaryFile(suffix=".bamod", delete=False) as tf:
                        tmp_pack = tf.name
                    textures = [(r["path"], r["file"], r["addr"]) if do_reg else (r["path"], r["file"])
                                for r in items]
                    create_texture_pack(tmp_pack, textures, bundle_kind=key)
                    q.put(("progress", "导入%s（%d 张）→ %s" % (disp, len(items), os.path.basename(bundle))))
                    n, _root, crc, removed = import_pack.import_pack(
                        tmp_pack, bundle, catalog,
                        progress=lambda m: q.put(("progress", m)), do_crc=do_crc)
                    try:
                        os.remove(tmp_pack)
                    except OSError:
                        pass
                    total += n
                    msgs.append("%s：%d 张（CRC %s）%s" % (
                        disp, len(items), crc if crc is not None else "跳过",
                        "，地址已注册" if do_reg else ""))
                q.put(("done", "图片导入完成（%d 个对象）：\n%s" % (total, "\n".join(msgs))))
            except Exception as e:  # noqa: BLE001
                q.put(("err", "导入失败：%s" % e))

        def poll():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "progress":
                        self.status.set("正在导入… %s" % val)
                    else:
                        self.status.set(val.split("\n")[0])
                        (messagebox.showerror if kind == "err" else messagebox.showinfo)("结果", val)
                        return
            except _queue.Empty:
                pass
            self.after(200, poll)

        threading.Thread(target=work, daemon=True).start()
        self.after(200, poll)
