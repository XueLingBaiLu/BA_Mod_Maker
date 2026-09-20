# -*- coding: utf-8 -*-
r"""[配方-04] **「补后坐力节点」面板** —— 给某件武器补一个 `recoil_<i>` 节点（纯观感增强）。

为什么要单独一个面板（doc17 ★⑳ / `[配方-04]` 产品化缺口）
==========================================================
工具 `_rev_tools\add_recoil_point.py` 以前**只在源码包里**，而且硬依赖三样**只有逆向工作台才有**的素材
（`dump.cs` 73 MB / DB 实况导出 `db_live` / 纯净包备份 3.15 GB）⇒ **只装 exe 的用户既找不到也跑不了**。
⇒ 本面板把这条配方搬进 GUI，并**吃掉三条硬依赖**：
  · `dump.cs` 缺 ⇒ 走**免 dump 读法**（已在真包 1107 个 `UnitPrefabTurretInfo` 上与 registry 逐条对过，一致 1107/0）
  · `db_live` 缺 ⇒ "DB 武器条数"如实打 **`未判定`**（⛔ 不当 0、也不拦下）；要严判就在面板里手填
  · 纯净包缺 ⇒ 源包默认退回**游戏目录**那份（**只读**：本面板从不写回源包，产物一律写到"输出包"）

机制（实证，见 `.re-kb\tools\add-recoil-point-recipe.md`）
========================================================
* `recoil_i` **找不到不抛异常**，只是那件武器**没有后坐力动画**（返回值直进
  `WeaponComponent.RecoilWeaponTransform` 0x70，无 null 检查）⇒ **观感增强，不是修 bug**。
* 名字契约：`recoil_` + **该武器在所在炮塔武器列表里的下标**；搜索根 = **那件武器自己的挂点**；
  引擎匹配走 `Assets.FindTransformBySubstring` = **只扫一层 + 子串**（`recoil_0_0` 也能命中 `recoil_0`）。
* ⚠⚠ **真正会崩的是另一条**：prefab 的 `UnitPrefabTurretInfo.Weapons` 数组必须 **≥ DB 里该炮塔的武器条数**
  （短了 ⇒ `IndexOutOfRangeException` ⇒ 日志 `CombatSystem initialization for unit=… failed`）。
  工具里这是**前置硬闸门**（命中即停、一个字节都不写）—— 本面板**保留**它，⛔ 不会因为"在 GUI 里"就放宽。

⛔ 本面板**不做**：替你扩容 `Weapons` 数组（那是 `add_turret.py` 那条线）、替你注册 catalog 地址
（出包后用「我的 bundle」管理器注册）、以及"验证实机效果"（判据 = **进游戏开火真的抖**，只有你能做）。
"""
import os
import sys
import threading


def _setup_paths():
    r"""把 `_rev_tools` 放进 `sys.path`（开发时是产品目录下那份；打包后由 exe 的 `_internal` 提供）。

    ⛔ 顺序与 `mod_assets._setup_paths()` 一致：`_rev_tools` 在前，
      否则会踩"同名模块撞车"（`技术资料\scripts\bundle_paths.py` 没有 `units_bundle`）那个坑。
    （本 docstring 必须是 **raw** —— 里面有 `\s`，普通字符串会报 `SyntaxWarning: invalid escape sequence`）
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "_rev_tools"),
              os.path.join(here, "blender_addon", "_rev_tools"),
              here):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return here


HERE = _setup_paths()


class RecoilModDialog(object):
    """「补后坐力节点（让某件武器有后坐力动画）」对话框。

    界面从下往上读：说明 → ① 源包 → ② 位置（prefab/炮塔/挂点/下标）→ ③ 输出与判据 →
    ④ 两个降级开关 → 按钮 → 日志框。
    """

    def __init__(self, master, app=None):
        import tkinter as tk
        from tkinter import ttk
        import add_recoil_point as AR          # ⛔ 只用它的 API，不碰 argparse 细节
        self.AR = AR
        self.tk, self.ttk, self.filedialog = tk, ttk, None
        self.app = app
        self.busy = False
        self._q = []
        self._after_id = None

        self.win = tk.Toplevel(master)
        self.win.title("补后坐力节点（[配方-04] · 让某件武器有后坐力动画）")
        self.win.geometry("1000x780")
        try:
            self.win.transient(master)
        except Exception:                                              # noqa: BLE001
            pass
        try:
            import ui_fit
            ui_fit.allow_maximize(self.win)
            ui_fit.fit_window(self.win)
        except Exception as exc:                                              # noqa: BLE001
            # ★ 兜底必打异常（依《授权与自治边界》§1 第 11 条项下 (d) 的反面：⛔ 不许把"自己崩了"吞成"看起来正常"）
            import sys as _sys
            _sys.stderr.write("⚠ ui_fit 窗口适配失败（不影响功能）：%s: %s\n" % (type(exc).__name__, exc))

        top = ttk.Frame(self.win, padding=10)
        top.pack(fill="both", expand=True)

        # ── 说明（把"什么用 / 会崩的是什么 / 本面板不做什么"写在脸上）──
        ttk.Label(top, text="★ 作用：给**某件武器**补一个 `recoil_<i>` 子节点 ⇒ 它有后坐力动画"
                            "（原版找不到就只是**不抖**，不报错）", foreground="#3ba55d").pack(anchor="w")
        ttk.Label(top, text="⚠ 真正会崩的是另一条：prefab 的 `UnitPrefabTurretInfo.Weapons` 数组必须 ≥ "
                            "DB 里该炮塔的武器条数 ⇒ 本面板**保留前置硬闸门**（命中即停、不写盘）",
                  foreground="#c08020").pack(anchor="w")
        ttk.Label(top, text="⛔ 本面板**不**替你扩容 Weapons 数组、**不**替你注册 catalog 地址；"
                            "判据 = 进游戏让那件武器开火、**真的抖**", foreground="#888888").pack(anchor="w")
        # ★ 2026-09-18（中枢批 · 候选甲）：★27 已**暂停**（用户「先不做这个」）⇒ **迁移未定**，
        #   此处只作中性说明，⛔ 不再预告"下批迁"；措辞逐字由中枢给定，⛔ 本线未自改字。
        #   与 子⑦ 的插件侧实现**配对**（两线同批都要改，⛔ 不许只改一边）。
        ttk.Label(top, text="★ 本功能仍在 exe 侧正常提供；后续形态正在评估中，如有调整会提前说明。",
                  foreground="#3ba55d").pack(anchor="w")

        # ── ① 源包 ──
        f1 = ttk.LabelFrame(top, text="① 源包（**只读**：本面板从不写回源包）", padding=8)
        f1.pack(fill="x", pady=(8, 0))
        self.bundle_var = tk.StringVar(value="")
        ttk.Entry(f1, textvariable=self.bundle_var).grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(f1, text="浏览…", command=self.browse_bundle).grid(row=0, column=1)
        ttk.Button(f1, text="自动检测", command=self.autodetect_bundle).grid(row=0, column=2, padx=(6, 0))
        f1.columnconfigure(0, weight=1)
        self.src_note = ttk.Label(f1, text="", foreground="#888888")
        self.src_note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ── ② 位置 ──
        f2 = ttk.LabelFrame(top, text="② 补到哪（下标 i = 该武器在**这个炮塔的武器列表**里的序号）", padding=8)
        f2.pack(fill="x", pady=(8, 0))
        ttk.Label(f2, text="单位模型 prefab：").grid(row=0, column=0, sticky="w")
        self.prefab_var = tk.StringVar(value="US_Rangers")
        ttk.Entry(f2, textvariable=self.prefab_var, width=28).grid(row=0, column=1, sticky="w")
        ttk.Label(f2, text="炮塔节点（留空=下标最大的那个）：").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.turret_var = tk.StringVar(value="")
        ttk.Entry(f2, textvariable=self.turret_var, width=16).grid(row=0, column=3, sticky="w")
        ttk.Label(f2, text="武器挂点：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mount_var = tk.StringVar(value="")
        ttk.Entry(f2, textvariable=self.mount_var, width=28).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f2, text="武器下标 i（要补 `recoil_i`）：").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(6, 0))
        self.index_var = tk.StringVar(value="")
        ttk.Entry(f2, textvariable=self.index_var, width=16).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Label(f2, text="（位置不知道就先点「① 体检」：它会把炮塔、挂点、每个挂点下的子节点全列出来）",
                  foreground="#888888").grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ── ③ 输出 ──
        f3 = ttk.LabelFrame(top, text="③ 输出包（产出的新包；源包一个字都不动）", padding=8)
        f3.pack(fill="x", pady=(8, 0))
        self.out_var = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self.out_var).grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(f3, text="另存为…", command=self.browse_out).grid(row=0, column=1)
        f3.columnconfigure(0, weight=1)
        self.space_note = ttk.Label(f3, text="", foreground="#888888")
        self.space_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # ── ④ 降级开关 ──
        f4 = ttk.LabelFrame(top, text="④ 素材降级（这台机器上没有逆向工作台素材时的行为）", padding=8)
        f4.pack(fill="x", pady=(8, 0))
        ttk.Label(f4, text="DB 武器条数（留空=自动查，查不到就如实报「未判定」）：").grid(row=0, column=0, sticky="w")
        self.dbw_var = tk.StringVar(value="")
        ttk.Entry(f4, textvariable=self.dbw_var, width=8).grid(row=0, column=1, sticky="w")
        self.noreg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f4, text="强制走免 dump 读法（不读 dump.cs；一般不用勾，缺它时会自动降级）",
                        variable=self.noreg_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(f4, text="⚠ 出包会写出与源包**同量级**的包（`units_assets_all` 级 = 2.4~3.4 GB，慢且占盘）⇒ "
                           "先点「① 体检」看清单，再决定要不要出包", foreground="#c08020").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ── 按钮 ──
        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=(8, 0))
        self.btn_check = ttk.Button(btns, text="① 体检（只读，先点这个）", command=self.run_check)
        self.btn_check.pack(side="left")
        self.btn_pack = ttk.Button(btns, text="② 出包（写新包）", command=self.run_pack)
        self.btn_pack.pack(side="left", padx=6)
        ttk.Button(btns, text="清空日志", command=self.clear_log).pack(side="left")
        self.state_lbl = ttk.Label(btns, text="就绪", foreground="#888888")
        self.state_lbl.pack(side="left", padx=10)

        # ── 日志 ──
        f5 = ttk.LabelFrame(top, text="日志（工具原样输出：源包/TI 读法/DB 武器条数/挂点清单/自检）", padding=6)
        f5.pack(fill="both", expand=True, pady=(8, 0))
        self.log_txt = tk.Text(f5, height=16, wrap="none")
        sb = ttk.Scrollbar(f5, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(side="left", fill="both", expand=True)

        self.autodetect_bundle(quiet=True)
        self._log("准备就绪。第一步：点「① 体检」（只读，不会写任何东西）。\n")

        # ★ [界面-03] 2026-09-18：**日志栏暗色**。以前 `self.log_txt` 是未上色的经典 `tk.Text`
        #   ⇒ 走系统默认 = **白底刺眼**（用户实测反馈："我指的是 `补后坐力节点` 工具的日志栏，背景是白色的刺眼"）。
        #   复用**既有正本** `ui_fit.theme_from_master`（与「我的 bundle」对话框同一机制，⛔ 不另起一套配色）：
        #   有主程序对象就继承它的调色板，取不到就 `ui_fit.DARK_FALLBACK` 兜底 ⇒ 日志栏暗底浅字，与应用其余部分一致。
        #   ⚠ 位置必须在**控件都建好之后**（`theme_from_master` 是递归给已存在的经典控件上色；
        #      放到 `fit_window` 旁边会因为日志框还没创建而**空跑**——本线实测踩过：那样读数仍是 SystemWindow）。
        #   ⛔ 独立 try：别让"上色失败"影响面板功能；失败时**打出来**（⛔ 不静默）。
        #   回归：`测试\test_ui03_recoil_log_dark.py`（配色读数改前 255/0 → 改后 暗/亮 ＋ 日志仍能追加 ＋ 裸 Text 亮底负向对照）。
        try:
            import ui_fit
            ui_fit.theme_from_master(self.win, master)
        except Exception as exc:                                              # noqa: BLE001
            import sys as _sys
            _sys.stderr.write("⚠ ui_fit 主题上色失败（不影响功能，但日志栏可能仍是白底）：%s: %s\n"
                              % (type(exc).__name__, exc))

    # ------------------------------------------------------------------ 工具
    def _log(self, s):
        """往日志框追加一行（★ 只允许**主线程**调用；工作线程请用 `_q` 队列）。"""
        try:
            self.log_txt.insert("end", str(s) + "\n")
            self.log_txt.see("end")
        except Exception:                                              # noqa: BLE001
            pass

    def _log_thread(self, s):
        self._q.append(str(s))                 # 线程安全：只 append，主线程来 drain

    def _drain(self):
        self._after_id = None
        while self._q:
            self._log(self._q.pop(0))
        if self.busy:
            self._after_id = self.win.after(120, self._drain)

    def clear_log(self):
        try:
            self.log_txt.delete("1.0", "end")
        except Exception:                                              # noqa: BLE001
            pass

    def _set_busy(self, on, label=""):
        self.busy = bool(on)
        st = "normal" if not on else "disabled"
        for b in (self.btn_check, self.btn_pack):
            try:
                b.configure(state=st)
            except Exception:                                          # noqa: BLE001
                pass
        try:
            self.state_lbl.configure(text=label or ("运行中…" if on else "就绪"))
        except Exception:                                              # noqa: BLE001
            pass
        if on and self._after_id is None:
            self._after_id = self.win.after(120, self._drain)

    # -------------------------------------------------------------- 文件选择
    def browse_bundle(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="选源包（**只读**，别选输出包）",
                                       filetypes=[("Unity bundle", "*.bundle"), ("全部", "*.*")])
        if p:
            self.bundle_var.set(p)
            self.src_note.configure(text="（手动指定）")

    def autodetect_bundle(self, quiet=False):
        """源包缺省来源：① 工作区 `备份\\` 纯净包 → ② **游戏目录**那份（只读）。"""
        p, why = self.AR._auto_bundle()
        if p:
            self.bundle_var.set(p)
            self.src_note.configure(text="自动检测：%s" % why)
            if not quiet:
                self._log("源包已自动检测：%s" % p)
        else:
            self.src_note.configure(text="⚠ 没找到：请手动指定（本机没装游戏 / 路径不同）")
            if not quiet:
                self._log("⚠ 自动检测没找到源包 —— 手工「浏览…」指一个 `units_assets_all_*.bundle`")

    def browse_out(self):
        from tkinter import filedialog
        p = filedialog.asksaveasfilename(title="输出包存到哪（**新文件**，别覆盖源包）",
                                         defaultextension=".bundle",
                                         initialfile="recoil_out.bundle",
                                         filetypes=[("Unity bundle", "*.bundle"), ("全部", "*.*")])
        if p:
            self.out_var.set(p)
            self._space_check()

    def _space_check(self):
        """★ 出包前把"要多少盘"说清楚（doc17 的风险项：units 级包出包会写 2.4~3.4 GB）。"""
        import shutil
        out = self.out_var.get().strip()
        src = self.bundle_var.get().strip()
        if not out or not src or not os.path.isfile(src):
            self.space_note.configure(text="")
            return
        d = os.path.dirname(os.path.abspath(out)) or "."
        try:
            free = shutil.disk_usage(d).free
        except Exception as e:                                         # noqa: BLE001
            self.space_note.configure(text="（量不到目标盘剩余空间：%s）" % e)
            return
        need = os.path.getsize(src)
        txt = "目标盘剩余 %.1f GB ／ 预计需要 ≈ %.1f GB（与源包同量级：%.1f GB）" % (
            free / 2 ** 30, need / 2 ** 30 * 1.05, need / 2 ** 30)
        self.space_note.configure(text=txt + ("　⛔ 空间不够" if free < need * 1.05 else "　✓ 空间够"),
                                  foreground="#c08055" if free < need * 1.05 else "#3ba55d")

    # ---------------------------------------------------------------- 参数组装
    def _collect(self, dry_run):
        from tkinter import messagebox
        src = self.bundle_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("缺源包", "请先指定一个存在的源包（或点「自动检测」）。")
            return None
        idx = None
        s = self.index_var.get().strip()
        if s:
            try:
                idx = int(s)
            except ValueError:
                messagebox.showwarning("下标不对", "武器下标 i 要是整数（例如 1）。")
                return None
        dbw = None
        s = self.dbw_var.get().strip()
        if s:
            try:
                dbw = int(s)
            except ValueError:
                messagebox.showwarning("条数不对", "DB 武器条数要填整数，或者留空让工具自己查。")
                return None
        if idx is None:
            if not dry_run:
                messagebox.showinfo("先体检", "还没填「武器下标 i」⇒ 现在只能体检。\n"
                                              "先点「① 体检」看挂点清单，再回来填下标后出包。")
                dry_run = True
        out = self.out_var.get().strip() or None
        if not dry_run and not out:
            messagebox.showwarning("缺输出包", "出包要指定「输出包」路径（点「另存为…」）。")
            return None
        if out and os.path.abspath(out) == os.path.abspath(src):
            messagebox.showwarning("别覆盖源包", "输出包不能等于源包 —— 换一个文件名。")
            return None
        return self.AR.build_args(
            bundle=src, prefab=self.prefab_var.get().strip() or "US_Rangers",
            turret=self.turret_var.get().strip() or None,
            mount=self.mount_var.get().strip() or None,
            index=idx, out=out, db_weapons=dbw,
            no_registry=bool(self.noreg_var.get()),
            dry_run=bool(dry_run))

    # ------------------------------------------------------------------ 执行
    def run_check(self):
        self._start(dry_run=True)

    def run_pack(self):
        self._start(dry_run=False)

    def _start(self, dry_run):
        if self.busy:
            return
        a = self._collect(dry_run)
        if a is None:
            return
        if not dry_run:
            self._space_check()
        self._log("")
        self._log("=" * 76)
        self._log("%s　prefab=%s turret=%s mount=%s index=%s"
                  % ("① 体检（只读）" if dry_run else "② 出包", a.prefab, a.turret, a.mount, a.index))
        self._set_busy(True, "运行中…（大包加载要 1~2 分钟）")
        t = threading.Thread(target=self._worker, args=(a,), daemon=True)
        t.start()

    def _worker(self, a):
        r"""★ 在工作线程里跑 `AR.run_recipe(...)`。

        ⛔ **不能**直接调 `AR.main()`：那个外壳最后是 `os._exit()` —— 在 GUI 里等于**把整个程序杀掉**。
        ⛔ `print` 也不能用：exe 是 `--windowed`，`sys.stdout is None` ⇒ `print` 会 `AttributeError`。
           ⇒ 本线程把 stdout/stderr 换成"往里塞队列"的写入器（只在这两样是 `None` 时才装）。
        """
        import contextlib
        rc, err = None, None

        class _W(object):
            def __init__(self, sink):
                self.sink = sink

            def write(self, s):
                for ln in str(s).splitlines():
                    if ln.strip():
                        self.sink(ln)

            def flush(self):
                pass

        old_out, old_err = sys.stdout, sys.stderr
        installed = False
        if old_out is None or old_err is None:
            sys.stdout = sys.stderr = _W(self._log_thread)
            installed = True
        try:
            rc = self.AR.run_recipe(a, log=self._log_thread)
        except self.AR.RecipeError as e:                    # 用户可读的失败 ⇒ 原样显示，不吐 traceback
            err = str(e)
            rc = 1
        except Exception as e:                              # noqa: BLE001
            import traceback
            err = "✗ 未预期的错误：%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc())
            rc = 1
        finally:
            if installed:
                sys.stdout, sys.stderr = old_out, old_err
            self._q.append("")
            self._q.append("—— 退出码 %s ——" % rc)
            if err:
                self._q.append(err)
            if rc == 0 and not a.dry_run:
                self._q.append("✓ 出包完成。**下一步**：① 用「我的 bundle」管理器把这个包注册地址、装进游戏；"
                               "② 进游戏让那件武器开火 ⇒ 判据 = **真的抖**（不加 recoil_* 也能开火、只是不抖）")
            elif rc == 0 and a.dry_run:
                self._q.append("✓ 体检完成（一个字节都没写）。确认「挂点清单」里的挂点名，再去 ② 出包。")
            elif rc == 2:
                self._q.append("⛔ 被**前置硬闸门**拦下（没写任何东西）：DB 武器条数 > prefab 的 Weapons 数组长度。"
                               "先去把数组扩容（`add_turret.py` 那条线），或把「DB 武器条数」改成实测值。")
            self.win.after(120, lambda: self._set_busy(False, "完成（退出码 %s）" % rc))
