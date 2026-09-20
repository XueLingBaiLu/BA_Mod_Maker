# -*- coding: utf-8 -*-
r"""[配方-05] **数值 mod 面板**：改游戏数值 → 打成 `configs.zip` → 放进场景文件夹 → 出货前自查。

为什么单独一个面板（待办清单 [配方-05]）
========================================
`Scenarios\<场景名>\configs.zip` 这条路**实机验证过**（Test 场景资金 7777 vs 原版 1000）⇒
它是**唯一一条"用户不用重下 6.8 GB、也不会被 Steam 校验还原"的发布路线**，
产物几十字节、还能**跟着 Steam 工坊的场景一起发** ✓

⚠ **本面板不做的事**（如实写在界面上）：**不从游戏资产里导全量模板**。
   游戏配置是 ScriptableObject，读它要一整套字段解码器（`game_config_tool.py` /
   `asset_field_decode.py`），那些都在 `技术资料\`、**不进交付件** ⇒ 本面板的模板来源是
   ① 场景文件夹里**现成的** `configs.zip`、② 你自己的一份 JSON。
   （覆盖语义是 `JsonSerializer.Populate` **按字段合并** ⇒ 只写要改的那几个字段就够，
     本来也**不需要**全量模板 ✓）
"""
import os
import sys


def _setup_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    ws = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    for p in (os.path.join(here, "blender_addon", "_rev_tools"), os.path.join(here, "_rev_tools"),
              here):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return here


_setup_paths()


class ConfigModDialog:
    """「数值 mod（场景 configs.zip）」对话框。"""

    def __init__(self, master, app=None, game_root=None):
        import tkinter as tk
        from tkinter import ttk
        import config_override as C
        self.C = C
        self.tk, self.ttk = tk, ttk
        self.app = app
        self.game_root = game_root or self._find_game_root()
        self.workdir = self._workdir()

        self.win = tk.Toplevel(master)
        self.win.title("数值 mod（场景 configs.zip）—— 改数值不用碰 6.8 GB 的 data.unity3d")
        self.win.geometry("980x760")
        self.win.transient(master)
        try:
            import ui_fit
            ui_fit.allow_maximize(self.win)
            ui_fit.fit_window(self.win)
        except Exception:                                             # noqa: BLE001
            pass

        top = ttk.Frame(self.win, padding=10)
        top.pack(fill="both", expand=True)

        # ── 说明（把"做什么/不做什么/生效条件"写在脸上）──
        ttk.Label(top, text="★ 这条路：改数值 → `configs.zip` → 放进**场景文件夹** ⇒ 不用重下 6.8 GB、"
                            "也不会被 Steam 校验还原", foreground="#3ba55d").pack(anchor="w")
        ttk.Label(top, text="⛔ 生效条件：**单机 / Scenario(2) / Skirmish(3)**（联机时游戏直接拒绝加载）· "
                            "⛔ 本面板**不导全量模板**（覆盖是按字段合并，只写要改的字段就够）",
                  foreground="#c08020").pack(anchor="w")

        # ── ① 场景 ──
        f1 = ttk.LabelFrame(top, text="① 场景文件夹（configs.zip 的落点）", padding=8)
        f1.pack(fill="x", pady=(8, 0))
        self.scen_var = tk.StringVar(value="")
        self.scen_box = ttk.Combobox(f1, textvariable=self.scen_var, width=78, values=[])
        self.scen_box.grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(f1, text="刷新场景列表", command=self.refresh_scenarios).grid(row=0, column=1)
        ttk.Button(f1, text="载入它的 configs.zip 当模板", command=self.load_from_scenario).grid(
            row=0, column=2, padx=(6, 0))
        f1.columnconfigure(0, weight=1)

        # ── ② 字段 ──
        f2 = ttk.LabelFrame(top, text="② 要改的数值（一行一个：`字段名 = 值`，`#` 开头是注释）", padding=8)
        f2.pack(fill="both", expand=True, pady=(8, 0))
        f2b = ttk.Frame(f2)
        f2b.pack(fill="x")
        ttk.Label(f2b, text="配置名：").pack(side="left")
        self.name_var = tk.StringVar(value="GameConfig")
        ttk.Entry(f2b, textvariable=self.name_var, width=30).pack(side="left")
        ttk.Button(f2b, text="检查这个名字能不能覆盖", command=self.check_name).pack(side="left", padx=6)
        ttk.Button(f2b, text="载入一份 JSON 当模板…", command=self.browse_json).pack(side="left")
        self.note = ttk.Label(f2b, text="", foreground="#888888")
        self.note.pack(side="left", padx=8)
        self.fields = tk.Text(f2, height=14, wrap="none")
        self.fields.pack(fill="both", expand=True, pady=(6, 0))
        self.fields.insert("1.0", "# 只写你要改的字段（覆盖 = 按字段合并）\n"
                                  "# 例子：\nDefaultPlayerMoney = 7777\n")

        # ── ③ 打包 / 放进去 / 自查 ──
        f3 = ttk.LabelFrame(top, text="③ 打包 → 放进场景 → 出货前自查", padding=8)
        f3.pack(fill="x", pady=(8, 0))
        self.out_var = tk.StringVar(value=os.path.join(self.workdir, "configs.zip"))
        ttk.Entry(f3, textvariable=self.out_var, width=70).grid(row=0, column=0, columnspan=3, sticky="we")
        ttk.Button(f3, text="📦 打包 configs.zip", command=self.do_pack).grid(row=0, column=3, padx=6)
        ttk.Button(f3, text="📥 放进选中的场景", command=self.do_install).grid(row=1, column=0, sticky="w",
                                                                          pady=(6, 0))
        ttk.Button(f3, text="✅ 出货前自查（黑名单 + 字段集）", command=self.do_check).grid(
            row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Button(f3, text="📖 黑名单是什么/怎么刷新", command=self.show_blocked).grid(
            row=1, column=2, sticky="w", padx=6, pady=(6, 0))
        f3.columnconfigure(2, weight=1)

        bottom = ttk.Frame(top)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bottom, text="关闭", command=self.win.destroy).pack(anchor="e")
        self.log = tk.Text(top, height=8, wrap="none")
        self.log.pack(fill="both", expand=True, pady=(8, 0))

        try:
            import ui_fit
            ui_fit.theme_from_master(self.win, master)
        except Exception:                                             # noqa: BLE001
            pass
        self.say("提示：先「刷新场景列表」→ 选一个场景 → 「载入它的 configs.zip 当模板」（没有就手写字段）")
        self.say("      → 改数值 → 「打包」→「放进选中的场景」→ 「出货前自查」✓")
        self.refresh_scenarios()

    # ---- 环境 ----
    def _find_game_root(self):
        r"""游戏目录：产品里有两个口径（`mod_paths._game_dir()` 与 `my_bundle.find_game_root()`）。

        ⛔ `mod_paths` 的**函数名是 `_game_dir`**（带下划线）—— 第一版写成 `game_dir()` 就抛
          `AttributeError`，然后一路落到兜底分支（功能没坏，但**白跑一次**）✗ ⇒ 两个都试 ✓
        """
        try:
            import mod_paths
            p = mod_paths._game_dir()
            if p:
                return p
        except Exception:                                             # noqa: BLE001
            pass
        try:
            import my_bundle as MB
            return MB.find_game_root()
        except Exception:                                             # noqa: BLE001
            return None

    def _workdir(self):
        try:
            import mod_paths
            return mod_paths.workdir()
        except Exception:                                             # noqa: BLE001
            return os.path.dirname(os.path.abspath("."))

    def say(self, msg):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")

    # ---- 动作 ----
    def refresh_scenarios(self):
        dirs = self.C.scenario_dirs(self.game_root)
        self._dirs = dirs
        vals = ["%s ⟵ %s%s" % (os.path.basename(sp), label,
                              "（已有 configs.zip）" if has else "")
                for label, sp, has, _m, _b in dirs]
        self._by_label = dict(zip(vals, [d[1] for d in dirs]))
        self.scen_box.configure(values=vals)
        if vals and not self.scen_var.get():
            self.scen_var.set(vals[0])
        self.say("场景文件夹 %d 个%s" % (len(vals), ("（游戏目录：%s）" % self.game_root) if self.game_root
                                      else "（**没找到游戏目录**）"))
        for v in vals[:12]:
            self.say("   · %s" % v)

    def _cur_scenario(self):
        return self._by_label.get(self.scen_var.get())

    def load_from_scenario(self):
        sp = self._cur_scenario()
        if not sp:
            self.say("✗ 先选一个场景文件夹")
            return
        z = os.path.join(sp, "configs.zip")
        if not os.path.isfile(z):
            self.say("（这个场景里还没有 configs.zip ⇒ 手写字段，或选一个已有的场景）")
            return
        try:
            packs = self.C.read_pack(z)
        except Exception as e:                                        # noqa: BLE001
            self.say("✗ 读不出来：%s: %s" % (type(e).__name__, e))
            return
        name = sorted(packs)[0]
        self.name_var.set(name)
        self.fields.delete("1.0", "end")
        self.fields.insert("1.0", self.C.format_fields(packs[name]) + "\n")
        self.say("✓ 已载入 %s 里的 %s（%d 个字段）—— 这就是你上次改的内容 ✓"
                 % (os.path.basename(z), name, len(packs[name])))

    def browse_json(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="载入一份 JSON 当模板",
                                       filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if not p:
            return
        try:
            packs = self.C.read_pack(p)
            name = sorted(packs)[0]
            self.name_var.set(name)
            self.fields.delete("1.0", "end")
            self.fields.insert("1.0", self.C.format_fields(packs[name]) + "\n")
            self.say("✓ 载入 %s（配置名 %s，%d 个字段）" % (os.path.basename(p), name, len(packs[name])))
        except Exception as e:                                        # noqa: BLE001
            self.say("✗ %s: %s" % (type(e).__name__, e))

    def check_name(self):
        blocked, src = self.C.blocked_names(log=self.say)
        name = self.name_var.get().strip()
        probs = self.C.check_name(name, blocked, is_path=False)
        self.say("黑名单来源：%s" % src)
        if not probs:
            self.say("✓ `%s` 名字合法、也不在黑名单里 ⇒ 这条路可用" % name)
        for sev, msg in probs:
            self.say("  %s %s" % ("✗" if sev == "error" else "⚠", msg))
        self.note.configure(text=("✓ 可覆盖" if not probs else "✗ 有问题"),
                            foreground=("#3ba55d" if not probs else "#e05555"))

    def _collect(self):
        obj, errs = self.C.parse_fields(self.fields.get("1.0", "end"))
        for e in errs:
            self.say("⚠ %s" % e)
        return obj

    def do_pack(self):
        name = self.name_var.get().strip()
        obj = self._collect()
        if not obj:
            self.say("✗ 一个字段都没填（格式：`字段名 = 值`）")
            return
        self.say("要打包：%s（%d 个字段）" % (name, len(obj)))
        blocked, _src = self.C.blocked_names(log=self.say)
        r = self.C.pack(self.out_var.get(), {name: obj}, blocked=blocked, log=self.say)
        self.say("打包结果：%s" % ("✓ 成功" if r["ok"] else "✗ 被拦下（看上面的 error；确认无误可勾 force）"))

    def do_install(self):
        sp = self._cur_scenario()
        z = self.out_var.get()
        if not sp:
            self.say("✗ 先选一个场景文件夹")
            return
        try:
            r = self.C.install_to_scenario(z, sp, log=self.say)
        except Exception as e:                                        # noqa: BLE001
            self.say("✗ %s: %s" % (type(e).__name__, e))
            return
        self.say("   放好了：%s" % r["dst"])

    def do_check(self):
        blocked, src = self.C.blocked_names(log=self.say)
        self.say("黑名单来源：%s" % src)
        self.C.selfcheck(self.out_var.get(), blocked=blocked, log=self.say)

    def show_blocked(self):
        blocked, src = self.C.blocked_names(log=self.say)
        self.say("来源：%s" % src)
        self.say("刷新命令（在工作区里跑一次）：%s" % self.C.blocked_source())
        if blocked:
            names = sorted(blocked)
            self.say("⛔ 不可覆盖的 %d 个配置名：" % len(names))
            for i in range(0, len(names), 4):
                self.say("   " + "  ".join("%-26s" % x for x in names[i:i + 4]))
            self.say("   （`GameConfig` **不在**名单里 ⇒ 资金/收入这类数值可以走这条路 ✓）")
        else:
            self.say("⚠ 本机没有黑名单快照 ⇒ 「这个名字能不能覆盖」**这一步没查**（不假装通过）")


def open_dialog(master, app=None):
    return ConfigModDialog(master, app=app)


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.geometry("1x1+0+0")
    d = ConfigModDialog(root)
    root.mainloop()
