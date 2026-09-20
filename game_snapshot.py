# -*- coding: utf-8 -*-
r"""**游戏文件快照**：把 `data.unity3d` / `catalog.json` / `GameAssembly.dll` 备份到一处，随时整份还原。

为什么需要它（2026-10 第 68 轮）
================================
改 `data.unity3d`（6.8 GB）是这套工具里**风险最高**的操作：写坏了没有回滚点，只能让 Steam
"验证文件完整性"重下几 GB ✗。产品里现有的自动备份是**针对 bundle 的**、而且**默认关闭**
（实测那份备份会白占 6.95 GB）⇒ 这里给一条**显式、可选、可删**的安全网 ✓

设计取舍（都想清楚了）
* **不自动备份 6.8 GB**：改成"用户主动建一次快照" ✓（建一次可以反复用）
* **指纹不算全文件 md5**：6.8 GB 全量哈希要几十秒~几分钟 ✗ ⇒ 用 **大小 + mtime + 首尾各 1 MB 的 blake2b**
  （够判断"是不是同一份文件 / 有没有被换过" ✓，并在文档里明说这不是全量哈希）
* **还原前先校验**：快照里文件的大小/指纹与 manifest 对不上就**拒绝还原**（免得用半截快照盖掉好文件）✓
* **游戏在跑就拒绝**（写 `data.unity3d` 会被占用）✓
* **建快照只读游戏目录**：只往快照目录写 ✓（测试会验证"游戏目录没被动过"）

用法
====
    python game_snapshot.py create                      # 在默认位置建一份快照（会先检查磁盘余量）
    python game_snapshot.py create --dest D:\ba_snap    # 指定位置
    python game_snapshot.py list                        # 列出已有快照（大小/时间）
    python game_snapshot.py verify <快照目录>            # 只读校验：快照文件与 manifest 是否一致
    python game_snapshot.py restore <快照目录>           # 还原（默认先 dry-run 打印计划，--apply 才真写）
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_FILES = ("BrokenArrow_Data/data.unity3d",
                 "BrokenArrow_Data/StreamingAssets/aa/catalog.json",
                 "GameAssembly.dll")
_GAME_ROOTS = [
    "D:/Steam/steamapps/common/broken_arrow",
    "C:/Program Files (x86)/Steam/steamapps/common/broken_arrow",
    "C:/Program Files/Steam/steamapps/common/broken_arrow",
    "E:/Steam/steamapps/common/broken_arrow",
    "F:/Steam/steamapps/common/broken_arrow",
]
def _default_snapshot_root():
    """快照默认目录 —— **绝不落 C 盘**（单份 6.8 GB！2026-10 修；见 `mod_paths.py`）"""
    try:
        import mod_paths
        return mod_paths.snapshot_root()
    except Exception:                                        # noqa: BLE001
        env = os.environ.get("BAMOD_HOME")
        if env:
            return os.path.join(env, "snapshots")
        for d in ("D:", "E:"):
            if os.path.isdir(d + os.sep):
                return os.path.join(d + os.sep, "BrokenArrow_Mods", "snapshots")
        return os.path.join(os.path.expanduser("~"), "BrokenArrow_Mods", "snapshots")


DEFAULT_ROOT = _default_snapshot_root()


def _looks_like_game(d):
    return bool(d) and os.path.isdir(os.path.join(d, "BrokenArrow_Data", "StreamingAssets", "aa", "PC"))


def find_game_root(explicit=None):
    """找游戏根目录。⛔ 显式传了 `explicit` 却不像游戏目录 ⇒ **报错，绝不静默回退**
    （静默回退会在还原时写错目标目录 —— 这条是测试 `test_game_snapshot.py` 抓出来的 ✓）"""
    if explicit:
        if _looks_like_game(explicit):
            return explicit
        raise FileNotFoundError("--game-root 指定的目录不像游戏目录：%s\n"
                                "（找不到 BrokenArrow_Data\\StreamingAssets\\aa\\PC）" % explicit)
    for d in _GAME_ROOTS:
        if _looks_like_game(d):
            return d
    return None


def game_running():
    """游戏是否在运行（⛔ 装/还原前必须确认：`data.unity3d` 被占用时会写坏）"""
    for img in ("BrokenArrow.exe", "BrokenArrow-Win64-Shipping.exe"):
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % img],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError:
            return False
        if "BrokenArrow" in (r.stdout or ""):
            return True
    return False


def fingerprint(path, chunk=1 << 20):
    """size + mtime + 首尾各 1MB 的 blake2b ⇒ 指纹字典（**不是全量哈希**，够判断"同一份文件"）"""
    st = os.stat(path)
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if st.st_size > 2 * chunk:
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return {"size": st.st_size, "mtime": int(st.st_mtime), "fp": h.hexdigest()}


def full_hash(path, chunk=8 << 20):
    """全量 sha256（6.6 GB 约 5 秒）。**还原的"是否已一样"判定必须用它** ——
    ⛔ 2026-10 实测教训：廉价指纹（首尾各 1 MB）对**中段**改动完全看不见，
       `GameAssembly.dll` 打了补丁却被指纹判成"和原始一致"，用它决定跳过就会漏还原 ✗"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _probe_dir(p):
    """`shutil.disk_usage` 的路径**必须已存在** ⇒ 往上找最近的已存在祖先目录"""
    p = os.path.abspath(p)
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p or "."


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def _resolve(root, rel):
    """在 `root` 下找文件：先按相对路径，再退回**按文件名**找
    （⇒ 可以拿"只存了 basename 的散备件目录"当快照源，例如 `_rev_tools/out/pristine/`）"""
    p = os.path.join(root, rel.replace("/", os.sep))
    if os.path.exists(p):
        return p
    alt = os.path.join(root, os.path.basename(rel))
    return alt if os.path.exists(alt) else None


def create_snapshot(game_root=None, dest=None, files=DEFAULT_FILES, log=print, allow_running=False,
                    src_root=None, deep=True):
    """建快照。`src_root` = 从哪读文件（默认游戏目录；可指向"原始备份目录"造一份标准基线快照）。
    `deep=True` 时顺便算全量 sha256（6.6 GB 约 5 秒，**还原的跳过判定靠它**）。"""
    root = find_game_root(game_root)
    if not root:
        raise FileNotFoundError("没找到游戏目录（用 --game-root 指定）")
    read_root = src_root or root
    if game_running() and not allow_running:
        raise RuntimeError("游戏正在运行 ⇒ 先退出游戏再建快照（`data.unity3d` 会被占用/写坏）")
    ts = time.strftime("%Y%m%d_%H%M%S")
    snap = dest or os.path.join(DEFAULT_ROOT, "snap_%s" % ts)
    man = {"version": 2, "created": ts, "game_root": root, "src_root": read_root,
           "note": ("每份文件记录 size/mtime/首尾1MB blake2b（快查用）+ sha256（全量，还原判定用）。"
                    "⛔ 只有 sha256 能发现**中段**改动"),
           "files": {}}
    total = 0
    for rel in files:
        src = _resolve(read_root, rel)
        if not src:
            log("  ⚠ 跳过（找不到）：%s" % rel)
            continue
        total += os.path.getsize(src)
    free = shutil.disk_usage(_probe_dir(os.path.dirname(os.path.abspath(snap)) or ".")).free
    if free < total * 1.05 + (1 << 30):
        raise RuntimeError("磁盘余量不足：需要约 %s，可用 %s" % (human(total), human(free)))
    os.makedirs(snap, exist_ok=True)
    log("建快照：读 %s → 存 %s（共 %s）" % (read_root, snap, human(total)))
    for rel in files:
        src = _resolve(read_root, rel)
        if not src:
            continue
        dst = os.path.join(snap, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        t0 = time.time()
        shutil.copy2(src, dst)
        man["files"][rel] = fingerprint(dst)                 # ★ 记录以**快照里那份**为准
        if deep:
            man["files"][rel]["sha256"] = full_hash(dst)
        man["files"][rel]["src_fp"] = fingerprint(src)["fp"]  # 顺便记源文件指纹（仅供对照）
        man["files"][rel]["src_name"] = os.path.basename(src)
        log("  ✓ %-52s %-10s %.1fs" % (rel, human(os.path.getsize(dst)), time.time() - t0))
    with open(os.path.join(snap, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    log("✓ 快照完成：%s（还原命令：python game_snapshot.py restore \"%s\" --apply）" % (snap, snap))
    return {"snapshot": snap, "manifest": man, "bytes": total}


def list_snapshots(root=None):
    out = []
    for d in sorted(glob.glob(os.path.join(root or DEFAULT_ROOT, "*"))):
        m = os.path.join(d, "manifest.json")
        if not os.path.exists(m):
            continue
        try:
            man = json.load(open(m, encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        size = sum(v.get("size", 0) for v in man.get("files", {}).values())
        out.append((d, man.get("created", "?"), size, sorted(man.get("files", {}) )))
    return out


def verify_snapshot(snap, log=print, deep=True):
    """校验快照自身是否完好。`deep=True`（默认）算全量 sha256 —— 唯一能发现中段损坏的办法。"""
    m = os.path.join(snap, "manifest.json")
    if not os.path.exists(m):
        log("✗ %s 不是一份快照（没有 manifest.json）" % snap)
        return False
    man = json.load(open(m, encoding="utf-8"))
    ok = True
    for rel, meta in sorted(man.get("files", {}).items()):
        p = os.path.join(snap, rel.replace("/", os.sep))
        if not os.path.exists(p):
            log("  ✗ 缺文件：%s" % rel)
            ok = False
            continue
        cur = fingerprint(p)
        h = full_hash(p) if (deep and meta.get("sha256")) else None
        same = (cur["size"] == meta.get("size") and cur["fp"] == meta.get("fp")
                and (h is None or h == meta.get("sha256")))
        log("  %s %-52s %s" % ("✓" if same else "✗", rel,
                               ("一致（%s）" % ("全量 sha256" if h else "快查指纹")) if same
                               else "对不上（大小 %s/%s）" % (cur["size"], meta.get("size"))))
        ok = ok and same
    log("%s 快照校验：%s" % ("✓" if ok else "✗", "通过" if ok else "**不通过**（别用它还原）"))
    return ok


def restore_snapshot(snap, game_root=None, apply=False, log=print, allow_running=False):
    if not verify_snapshot(snap, log=lambda *a: None, deep=False):
        raise RuntimeError("快照校验不通过 ⇒ 拒绝还原（免得用半截快照盖掉好文件）")
    root = find_game_root(game_root)
    if not root:
        raise FileNotFoundError("没找到游戏目录")
    if game_running() and not allow_running:
        raise RuntimeError("游戏正在运行 ⇒ 先退出游戏再还原")
    man = json.load(open(os.path.join(snap, "manifest.json"), encoding="utf-8"))
    log("准备还原到 %s（源快照 %s）" % (root, snap))
    for rel, meta in sorted(man.get("files", {}).items()):
        src = os.path.join(snap, rel.replace("/", os.sep))
        dst = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(src):
            continue
        # ★ 跳过判定用**全量** sha256：廉价指纹对中段改动看不见 ⇒ 会漏还原（实测踩过）
        same = False
        if os.path.exists(dst) and os.path.getsize(dst) == meta.get("size"):
            if meta.get("sha256"):
                same = full_hash(dst) == meta["sha256"]
            else:
                same = fingerprint(dst)["fp"] == meta.get("fp")
        log("  %s %s%s" % ("=" if same else "→", rel, "（已经是这份，跳过）" if same else ""))
        if same or not apply:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".restore_tmp"
        shutil.copy2(src, tmp)                                   # 先写临时文件再原子替换 ✓
        os.replace(tmp, dst)
        log("     ✓ 已还原 %s（%s）" % (rel, human(os.path.getsize(dst))))
    if not apply:
        log("（预演；确认无误后加 --apply 才真的写回）")
    return True


# ---------------------------------------------------------------------------
# GUI（Tk）：建快照 / 列表 / 校验 / 还原。复制 6.8 GB 会卡界面 ⇒ 丢后台线程 + 队列回传日志
# ---------------------------------------------------------------------------
class GameSnapshotDialog:
    def __init__(self, parent, app=None):
        import queue
        import threading
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.q = queue.Queue()
        self.threading = threading
        self.worker = None
        self.win = tk.Toplevel(parent)
        self.win.title("游戏文件快照（安全网：备份 / 还原）")
        self.win.geometry("1000x720")
        self.win.transient(parent)

        # 底栏先 pack（★ 踩过的坑：后 pack 的控件会被上面的日志区挤出窗口）
        bar = tk.Frame(self.win)
        bar.pack(side="bottom", fill="x", padx=8, pady=6)
        tk.Button(bar, text="关闭", width=12, command=self.win.destroy).pack(side="right")
        self.status = tk.Label(bar, text="就绪", anchor="w", fg="#333")
        self.status.pack(side="left", fill="x", expand=True)

        top = tk.Frame(self.win)
        top.pack(side="top", fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="快照存放目录：").pack(side="left")
        self.root_var = tk.StringVar(value=DEFAULT_ROOT)
        tk.Entry(top, textvariable=self.root_var).pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(top, text="浏览…", command=self._pick_root).pack(side="left")

        row = tk.Frame(self.win)
        row.pack(side="top", fill="x", padx=8, pady=4)
        self.btn_create = tk.Button(row, text="① 建快照（备份游戏文件）", width=26,
                                    command=self.do_create)
        self.btn_create.pack(side="left")
        tk.Button(row, text="② 校验选中快照", width=16, command=self.do_verify).pack(side="left", padx=4)
        tk.Button(row, text="③ 还原（预演）", width=14, command=lambda: self.do_restore(False)).pack(side="left")
        tk.Button(row, text="④ 还原（真写回）", width=16,
                  command=lambda: self.do_restore(True)).pack(side="left", padx=4)
        tk.Button(row, text="刷新列表", width=10, command=self.refresh).pack(side="left", padx=4)
        tk.Button(row, text="删除选中快照", width=14, command=self.do_delete).pack(side="left")

        info = tk.Label(self.win, anchor="w", justify="left", fg="#444",
                        text=("备份内容：BrokenArrow_Data/data.unity3d（约 6.6 GB）、"
                              "StreamingAssets/aa/catalog.json、GameAssembly.dll\n"
                              "每份文件记 size/mtime/首尾1MB 指纹 + 全量 sha256（还原的跳过判定用全量，"
                              "⛔ 指纹查不出中段改动）\n"
                              "还原前必先校验 · 预演不写任何东西 · 游戏在运行时会拒绝操作"))
        info.pack(side="top", fill="x", padx=8)

        mid = tk.Frame(self.win)
        mid.pack(side="top", fill="both", expand=True, padx=8, pady=6)
        cols = ("created", "size", "path")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=6)
        for c, t, w in (("created", "建立时间", 150), ("size", "占用", 100), ("path", "路径", 700)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="top", fill="both", expand=True)

        self.log = tk.Text(self.win, height=18, wrap="none")
        self.log.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 6))
        self.log.configure(state="disabled")

        self.refresh()
        self.win.after(100, self._drain)

        # ★ v1.8.112：**深色主题** —— `tk.Text`（日志框）是**经典 tk 控件，不跟随 ttk 主题**
        #   ⇒ 不显式上色就是刺眼的 `SystemWindow` 白底（「我的 bundle」②/④ 被用户点出来过，
        #     顺手把同类漏掉的对话框一起补上，回归见 `测试\test_dialog_dark_theme.py`）✓
        try:
            import ui_fit
            ui_fit.theme_from_master(self.win, parent)
        except Exception:  # noqa: BLE001 - 主题失败不影响功能
            pass

    # --- 小工具 ---
    def _log(self, *a):
        self.q.put(" ".join(str(x) for x in a))

    def _drain(self):
        dirty = False
        try:
            while True:
                msg = self.q.get_nowait()
                if msg == "\x00DONE":
                    dirty = True
                    self._set_busy(False)
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
                self.status.configure(text=msg[:80])
        except Exception:                                            # noqa: BLE001
            pass
        if dirty:
            self.refresh()
        self.win.after(100, self._drain)

    def _set_busy(self, busy, text=None):
        st = "disabled" if busy else "normal"
        for w in (self.btn_create,):
            w.configure(state=st)
        if text:
            self.status.configure(text=text)

    def _pick_root(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="快照存放目录", initialdir=self.root_var.get())
        if d:
            self.root_var.set(d)

    def _run(self, fn, label):
        if self.worker and self.worker.is_alive():
            self._log("⚠ 上一个任务还在跑，等它结束")
            return
        self._set_busy(True, label)

        def job():
            try:
                fn()
            except Exception as e:                                   # noqa: BLE001
                self._log("✗ %s: %s" % (type(e).__name__, e))
            finally:
                self.q.put("\x00DONE")
        self.worker = self.threading.Thread(target=job, daemon=True)
        self.worker.start()

    def _sel(self):
        s = self.tree.selection()
        return self.tree.item(s[0], "values")[2] if s else None

    # --- 动作 ---
    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        try:
            rows = list_snapshots(self.root_var.get())
        except Exception as e:                                       # noqa: BLE001
            self._log("✗ 读列表失败：%s" % e)
            return
        for d, ts, size, files in rows:
            self.tree.insert("", "end", values=(ts, human(size), d))
        self.status.configure(text="共 %d 份快照" % len(rows))

    def do_create(self):
        root = find_game_root()
        if not root:
            self._log("✗ 没找到游戏目录")
            return
        dest = os.path.join(self.root_var.get(), "snap_" + time.strftime("%Y%m%d_%H%M%S"))

        def work():
            self._log("游戏目录：%s" % root)
            self._log("快照目录：%s" % dest)
            create_snapshot(game_root=root, dest=dest, log=self._log)
        self._run(work, "正在建快照…（6.8 GB，几分钟）")

    def do_verify(self):
        d = self._sel()
        if not d:
            self._log("⚠ 先在列表里选中一份快照")
            return
        self._run(lambda: verify_snapshot(d, log=self._log), "正在校验…")

    def do_restore(self, apply_):
        d = self._sel()
        if not d:
            self._log("⚠ 先在列表里选中一份快照")
            return
        if apply_:
            from tkinter import messagebox
            if not messagebox.askyesno("确认还原",
                                       "把游戏文件还原成这份快照？\n\n%s\n\n"
                                       "游戏必须已退出。当前装在游戏里的 mod 会被覆盖掉。" % d):
                return
            self._log("=== 真写回 ===")
        else:
            self._log("=== 预演（不写任何东西）===")
        self._run(lambda: restore_snapshot(d, apply=apply_, log=self._log), "正在还原…")

    def do_delete(self):
        d = self._sel()
        if not d:
            self._log("⚠ 先在列表里选中一份快照")
            return
        from tkinter import messagebox
        if not messagebox.askyesno("确认删除", "删除这份快照（不可恢复）？\n\n%s" % d):
            return
        try:
            shutil.rmtree(d)
            self._log("✓ 已删除 %s" % d)
        except Exception as e:                                       # noqa: BLE001
            self._log("✗ 删除失败：%s" % e)
        self.refresh()


def main():
    ap = argparse.ArgumentParser(description="游戏文件快照（备份 / 列表 / 校验 / 还原）")
    ap.add_argument("action", choices=("create", "list", "verify", "restore"))
    ap.add_argument("snapshot", nargs="?", help="verify/restore 用：快照目录")
    ap.add_argument("--game-root")
    ap.add_argument("--dest", help="create 用：快照放哪（默认 ~\\BrokenArrow_Mods\\snapshots\\snap_<ts>）")
    ap.add_argument("--root", help="list 用：快照根目录")
    ap.add_argument("--files", help="create 用：要备份的相对路径（逗号分隔）")
    ap.add_argument("--src-root", help="create 用：从哪读文件（默认游戏目录；可指向".strip() +
                    "只存了散备件的原始备份目录，例如 _rev_tools/out/pristine）")
    ap.add_argument("--quick", action="store_true", help="verify 用：只查快查指纹（不算全量 sha256，快但查不出中段损坏）")
    ap.add_argument("--apply", action="store_true", help="restore 真的写回（默认只预演）")
    a = ap.parse_args()
    try:
        if a.action == "create":
            files = tuple(x.strip() for x in a.files.split(",")) if a.files else DEFAULT_FILES
            r = create_snapshot(a.game_root, a.dest, files, log=print, src_root=a.src_root)
            return 0 if r else 1
        if a.action == "list":
            rows = list_snapshots(a.root)
            if not rows:
                print("（没有快照；用 `create` 建一份）")
                return 0
            for d, ts, size, files in rows:
                print("%-22s %-10s %s\n    %s" % (ts, human(size), d, "、".join(files)))
            return 0
        if a.action == "verify":
            return 0 if verify_snapshot(a.snapshot, log=print, deep=not a.quick) else 1
        if a.action == "restore":
            return 0 if restore_snapshot(a.snapshot, a.game_root, apply=a.apply, log=print) else 1
    except Exception as e:                                        # noqa: BLE001
        print("✗ %s: %s" % (type(e).__name__, e))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
