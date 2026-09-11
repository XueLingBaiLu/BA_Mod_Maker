# -*- coding: utf-8 -*-
"""添加音频/音效：把新声音注入 FMOD 音库（新事件 GUID + 样本 + strings bank 路径注册），
可选同时创建武器音效预设（注入 sfx bundle + catalog 注册），供 DB 的 AudioPreset 引用。

依赖：
- audio_bank.add_sound(sa, event_name, pcm16_wav, bank_name, progress)：
  往音库注入新事件并在 Master.strings.bank 注册路径，返回 (guid_hex, 样本数)。
- audio_preset.create_weapon_sound_preset(address, shot_event, impact_event, ...)：
  克隆武器音效预设资产 + catalog 注册 + CRC。
"""
import os
import sys
import shutil
import subprocess
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from audio_import import detect_streaming_assets, BACKUP_DIRNAME, backup_audio

EVENT_PREFIX = "event:/BrokenArrowGame/BA_Mod/"


def _find_ffmpeg():
    """返回 ffmpeg 可执行路径或 None（先查 PATH，再查工具同目录）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for base in os.environ.get("PATH", "").split(os.pathsep):
        if base:
            cands.append(os.path.join(base, "ffmpeg.exe"))
    cands.append(os.path.join(here, "ffmpeg.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _to_pcm16_wav(src, progress=None):
    """把任意常见音频转成 PCM16 WAV 字节。WAV(PCM) 直接读；其余走 ffmpeg。"""
    import wave
    ext = os.path.splitext(src)[1].lower()
    if ext == ".wav":
        try:
            with wave.open(src, "rb") as w:
                if w.getsampwidth() == 2 and w.getcomptype() == "NONE":
                    if progress:
                        progress("读取 WAV（PCM16，%d Hz，%d 声道）" % (w.getframerate(), w.getnchannels()))
                    with open(src, "rb") as f:
                        return f.read()
        except Exception:
            pass  # 交给 ffmpeg
    ff = _find_ffmpeg()
    if not ff:
        raise RuntimeError(
            "该格式需要 ffmpeg 解码（把 ffmpeg.exe 放到 PATH 或工具目录），"
            "或先用其他工具把音频转成 PCM 16-bit WAV。")
    if progress:
        progress("ffmpeg 转码为 PCM16 WAV ...")
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        r = subprocess.run(
            [ff, "-y", "-i", src, "-acodec", "pcm_s16le", tmp],
            capture_output=True, timeout=600)
        if r.returncode != 0 or not os.path.isfile(tmp):
            raise RuntimeError("ffmpeg 转码失败：%s" % r.stderr.decode("utf-8", "replace")[-400:])
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _sanitize(name):
    out = []
    for ch in name.strip():
        if ch.isalnum() or ch in "_-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "Sound"


class AudioAddDialog(tk.Toplevel):
    """添加新音效对话框。"""

    def __init__(self, master, sa=None):
        super().__init__(master)
        self.title("添加音频/音效（新事件 + 武器预设）")
        self.geometry("760x560")
        self.minsize(680, 480)
        self.transient(master)
        self.sa = sa or detect_streaming_assets()
        self.src_file = None

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="音库目录：%s" % (self.sa or "未检测到"),
                  foreground="#555").pack(anchor="w")

        # 音源
        src = ttk.LabelFrame(frm, text="1. 音频文件", padding=8)
        src.pack(fill="x", pady=(8, 4))
        self.src_lbl = ttk.Label(src, text="（未选择）", foreground="#777")
        self.src_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(src, text="选择…", command=self._pick_src).pack(side="right")

        # 事件名
        ev = ttk.LabelFrame(frm, text="2. 音效事件名", padding=8)
        ev.pack(fill="x", pady=4)
        ttk.Label(ev, text=EVENT_PREFIX).pack(side="left")
        self.name_var = tk.StringVar()
        ttk.Entry(ev, textvariable=self.name_var, width=34).pack(side="left", padx=(0, 8))
        ttk.Label(ev, text="（自动生成，游戏内按此名引用）",
                  foreground="#777").pack(side="left")

        # 目标音库
        bk = ttk.LabelFrame(frm, text="3. 写入音库", padding=8)
        bk.pack(fill="x", pady=4)
        ttk.Label(bk, text="目标音库：").pack(side="left")
        self.bank_var = tk.StringVar(value="CustomDialog.bank")
        self.bank_combo = ttk.Combobox(bk, textvariable=self.bank_var, width=26, state="readonly")
        self.bank_combo.pack(side="left", padx=(0, 8))
        ttk.Label(bk, text="（仅支持 CustomDialog.bank 空音库，避免覆盖其他音库）",
                  foreground="#777").pack(side="left")
        self._refresh_banks()

        # 武器预设
        wp = ttk.LabelFrame(frm, text="4. 武器音效预设（可选）", padding=8)
        wp.pack(fill="x", pady=4)
        self.wp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(wp, text="同时创建武器音效预设（开火/命中音，可在数据库编辑器中把武器 AudioPreset 指向它）",
                        variable=self.wp_var, command=self._toggle_wp).pack(anchor="w")
        wpf = ttk.Frame(wp)
        wpf.pack(fill="x", pady=(6, 0))
        ttk.Label(wpf, text="预设名：").grid(row=0, column=0, sticky="e")
        self.preset_var = tk.StringVar(value="WEAPON_SOUND_PRESET_MOD")
        self.preset_entry = ttk.Entry(wpf, textvariable=self.preset_var, width=34)
        self.preset_entry.grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(wpf, text="开火(Shot)：").grid(row=0, column=2, sticky="e")
        self.shot_var = tk.StringVar()
        self.shot_entry = ttk.Entry(wpf, textvariable=self.shot_var, width=36)
        self.shot_entry.grid(row=0, column=3, sticky="w", padx=(4, 0))
        ttk.Label(wpf, text="命中(Impact)：").grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.impact_var = tk.StringVar()
        ttk.Entry(wpf, textvariable=self.impact_var, width=36).grid(row=1, column=3, sticky="w", padx=(4, 0), pady=(4, 0))

        # 操作
        ops = ttk.Frame(frm)
        ops.pack(fill="x", pady=(10, 0))
        self.go_btn = ttk.Button(ops, text="添加音效", command=self._go)
        self.go_btn.pack(side="left")
        ttk.Button(ops, text="关闭", command=self.destroy).pack(side="right")

        # 输出
        self.log = tk.Text(frm, height=8, state="disabled", bg="#f6f6f6")
        self.log.pack(fill="both", expand=True, pady=(8, 0))

        self._toggle_wp()

    # ---------- 内部 ----------

    def _refresh_banks(self):
        self.bank_combo["values"] = ["CustomDialog.bank"]
        self.bank_var.set("CustomDialog.bank")

    def _pick_src(self):
        src = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频", "*.wav *.mp3 *.ogg *.flac *.m4a"), ("全部", "*.*")])
        if src:
            self.src_file = src
            self.src_lbl.config(text=os.path.basename(src))
            base = _sanitize(os.path.splitext(os.path.basename(src))[0])
            if not self.name_var.get():
                self.name_var.set(base)
                self._sync_events()

    def _sync_events(self):
        ev = EVENT_PREFIX + (_sanitize(self.name_var.get()))
        if not self.shot_var.get():
            self.shot_var.set(ev)
        if not self.impact_var.get():
            self.impact_var.set(ev + "_Impact")

    def _toggle_wp(self):
        for w in (self.preset_entry, self.shot_entry):
            pass  # 保留（子控件状态跟随 checkbutton 由用户自行填写）

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")
        self.update_idletasks()

    def _go(self):
        if not self.sa:
            messagebox.showerror("错误", "未检测到游戏 StreamingAssets 目录")
            return
        if not self.src_file:
            messagebox.showinfo("提示", "请先选择音频文件")
            return
        name = _sanitize(self.name_var.get())
        event = EVENT_PREFIX + name
        self._sync_events()
        self.go_btn.config(state="disabled")
        try:
            self._log("转码音频 ...")
            wav_bytes = _to_pcm16_wav(self.src_file, progress=self._log)
            import audio_bank
            self._log("注入音库 %s ..." % self.bank_var.get())
            guid, n_samples = audio_bank.add_sound(
                self.sa, event, wav_bytes,
                bank_name=self.bank_var.get(), progress=self._log)
            self._log("完成：事件 %s（GUID %s，样本 %d）" % (event, guid, n_samples))
            if self.wp_var.get():
                import audio_preset
                addr = _sanitize(self.preset_var.get())
                self._log("创建武器音效预设 %s ..." % addr)
                bundle, pid, crc, reg = audio_preset.create_weapon_sound_preset(
                    addr, self.shot_var.get(), self.impact_var.get(),
                    progress=self._log)
                self._log("预设完成：pid=%s CRC=%s 注册=%s（%s）" % (pid, crc, reg, bundle))
                self._log("下一步：在数据库编辑器中把武器行的 AudioPreset 改为 %s" % addr)
            messagebox.showinfo("完成", "新音效已添加：%s%s" % (event,
                                 "（含武器预设）" if self.wp_var.get() else ""))
        except Exception as e:  # noqa: BLE001
            self._log("失败：%s" % e)
            messagebox.showerror("添加失败", str(e))
        finally:
            self.go_btn.config(state="normal")


def open_dialog(master):
    return AudioAddDialog(master)


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    d = AudioAddDialog(root)
    root.mainloop()
