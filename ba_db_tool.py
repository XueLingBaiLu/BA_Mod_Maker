# -*- coding: utf-8 -*-
"""断箭 Broken Arrow 数据库编辑器 (Database Editor).

GUI:  python ba_db_tool.py            (or double-click 启动编辑器.bat)
CLI:  python ba_db_tool.py <command>  (decrypt / encrypt / validate / tables / find)

Edits the DataBaseCompiled asset exported from data.unity3d by UABEA, auto
re-encrypts on save (AES-256-CBC) and keeps the original file name so it can
be imported straight back with UABEA's "Import Dump".
UI languages: 中文 / English / Русский (F10).

----------------------------------------------------------------------------
维护速查（maintenance quick reference）
----------------------------------------------------------------------------
主要类：
  EditorApp       主窗口：表列表 / 搜索 / 列筛选 / 工具栏 / 名称查 Id
  DetailWindow    双击行弹出的钻取编辑器：字段表单 + 关联树 + 迷你编辑器
  LookupBox       名称→Id 即时下拉（主窗口与详情窗口复用）
  UndoStack       快照式撤销 / 重做

关键数据：
  self.tables         表名 → 行列表（解密后的全部数据）
  self._data_version  数据版本号，任何改动 +1 以失效缓存
  self._id_index      Id → 行下标；self._by_unit / _by_turret / _by_mod 为关系索引
                      （均由 _ensure_indexes() 惰性重建）

关联树：
  _build_relations_into   快照展开状态 → 重建 → 恢复（保证删除/编辑后不塌）
  _build_unit_relations   单位关联组装（技能/炮塔/武器/弹药/装甲/机动/传感器/改装）
  _build_generic_relations 普通表的入/出引用

折叠逻辑：DetailWindow._on_rel_click 只在点击行首彩色 emoji 图标或
          「▶/▼」标记时切换展开/折叠，点击正文只选中。
外键解析：FIELD_REF_MAP（字段→表）+ _row_name（Id→名称）+ _fk_update_label。
"""

import argparse
import json
import os
import shutil
import sys
import threading
import math

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)
# Optional: Pillow from the local offline mirror (source runs) so game icons
# can be scaled smoothly and exactly; ignored when absent (subsample fallback).
try:
    _upy = os.path.join(TOOL_DIR, "_unitypy")
    if os.path.isdir(_upy) and _upy not in sys.path:
        sys.path.insert(0, _upy)
except Exception:
    pass


def _bundled_data_dir():
    """Directory holding bundled data (icons / localization_map.json /
    clean_baseline.json / UABEADump). Source runs: TOOL_DIR. PyInstaller
    6.x onedir: sys._MEIPASS (the _internal folder)."""
    for d in (getattr(sys, "_MEIPASS", None), TOOL_DIR):
        if d and os.path.isdir(d):
            return d
    return TOOL_DIR

from ba_crypto import (  # noqa: E402
    TABLE_FIELDS, decrypt_tables, encrypt_tables, export_folder,
    import_folder, is_dump_obj, load_dump, table_name, write_dump,
)
from i18n import LANGS, LANG_NAMES, Translator  # noqa: E402
from ba_glossary import (  # noqa: E402
    field_info, table_info, enum_values, TABLES, FIELDS, COMMON, ENUMS, ORDER,
    # v1.8.55：枚举字段的绿色箭头导航（枚举键 / 含义 / 成员名 / 真实作用说明）
    enum_key, enum_label, enum_member, enum_note,
)
# 模型修改功能已整体迁移到 Blender 插件（BA Mod Maker Blender 插件 v2.0）：
# 模型导入/挂载点编辑/构建写回/CRC 计算都在 Blender 里做，本工具只保留数据表编辑。
APP_NAME = "BA Mod Maker"
try:
    from version import APP_VERSION  # 单一版本来源（version.py，改动时同步更新）
except ImportError:  # 打包环境兜底
    APP_VERSION = "1.6.2"


def app_title_text(tr):
    """窗口标题（含版本号，显示在左上角标题栏）。"""
    return "%s v%s" % (tr.t("app_title"), APP_VERSION)


MAX_UNDO = 100
TREE_CHUNK = 400          # rows inserted per UI cycle (keeps browsing smooth)
REL_CAP = 1000            # max relation items per group
FOLD_OPEN = "▼ "          # big fold indicator: expanded
FOLD_CLOSED = "▶ "        # big fold indicator: collapsed
# Fields whose edit can change the relation tree / window title; only edits to
# these need a relation-tree refresh (everything else refreshes incrementally).
REL_DISPLAY_FIELDS = ("Id", "Name", "HUDName", "UIName", "ModelFileName",
                      "CountryId", "Cost")

# Color emoji icons (Twemoji PNGs in ./icons). Tk's GDI renderer draws emoji
# characters in monochrome, so colored PNG images are used instead; if an
# icon file is missing, the tool falls back to the plain emoji character.
ICON_DIR = os.path.join(_bundled_data_dir(), "icons")
EMOJI = {
    "open": ("1f4c2", "📂"), "unity3d": ("1f4e6", "📦"), "import_unity3d": ("1f4e5", "📥"), "folder": ("1f4c1", "📁"), "save": ("1f4be", "💾"),
    "export": ("2b07", "⬇"), "undo": ("21a9", "↩"), "redo": ("21aa", "↪"),
    "add": ("2795", "➕"), "dup": ("1f4cb", "📋"), "del": ("1f5d1", "🗑"),
    "refs": ("1f517", "🔗"), "validate": ("2705", "✅"), "lookup": ("1f50d", "🔍"),
    "lang": ("1f310", "🌐"), "dict": ("1f4d6", "📖"), "back": ("2b05", "⬅"), "close": ("2716", "✖"),
    "clone": ("1f9ec", "🧬"), "apply": ("2705", "✅"),
    "abilities": ("26a1", "⚡"), "turrets": ("1f5fc", "🗼"),
    "infantry": ("1f465", "👥"), "ammo": ("1f4a3", "💣"), "armors": ("1f6e1", "🛡"),
    "mobility": ("1f3ce", "🏎"), "mods": ("1f9e9", "🧩"),
    "weapon": ("1f52b", "🔫"), "flight": ("2708", "✈"), "member": ("1f464", "👤"),
    "sensors": ("1f4e1", "📡"),
    "out": ("2197", "↗"), "in": ("2199", "↙"),
    "table": ("1f4ca", "📊"), "search": ("1f50e", "🔎"),
}
EMOJI_CHAR_TO_CODE = {char: codepoint for _name, (codepoint, char) in EMOJI.items()}
_icon_cache = {}


def icon_image(tk_root, codepoint, logical_size=18):
    """Colored emoji as a PhotoImage, or None if the icon is unavailable.

    The cache is keyed by (codepoint, size). Uses Pillow (LANCZOS) so the
    whole glyph is kept — integer subsample used to drop the bottom rows,
    clipping emoji like ⚡ / 🗼 ("底部缺像素").
    """
    key = (codepoint, logical_size)
    if key in _icon_cache:
        return _icon_cache[key]
    path = os.path.join(ICON_DIR, codepoint + ".png")
    if not os.path.exists(path):
        _icon_cache[key] = None
        return None
    try:
        import tkinter as _tk
        try:
            scaling = float(tk_root.tk.call("tk", "scaling"))
        except Exception:
            scaling = 1.0
        target = max(16, int(logical_size * scaling))
        img = None
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            if im.width != target or im.height != target:
                im = im.resize((target, target), Image.LANCZOS)
            import io
            buf = io.BytesIO()
            im.save(buf, "PNG")
            img = _tk.PhotoImage(data=buf.getvalue())
        except Exception:
            img = None
        if img is None:
            # packaged (no Pillow): integer subsample. Round the factor so the
            # emoji never stays at native size and overflows the row; the
            # 96x96 padded canvas keeps the glyph safe from bottom row-dropping.
            img = _tk.PhotoImage(file=path)
            factor = max(1, round(img.width() / max(target, 1)))
            if factor > 1:
                img = img.subsample(factor)
            while img.width() > max(target * 2, 24):
                img = img.subsample(2)
        _icon_cache[key] = img
        return img
    except Exception:
        _icon_cache[key] = None
        return None


# ---------------------------------------------------------------------------
# Game localization + extracted icon helpers (unit browser, slot/option/spec
# name localization, and resolution of the PNGs exported from the game bundles)
# ---------------------------------------------------------------------------
_LOCALE_MAP = None
_LOCALE_LOADED = False


def _load_locale_map():
    global _LOCALE_MAP, _LOCALE_LOADED
    if not _LOCALE_LOADED:
        _LOCALE_LOADED = True
        _LOCALE_MAP = {}
        try:
            with open(os.path.join(_bundled_data_dir(), "localization_map.json"), encoding="utf-8") as f:
                _LOCALE_MAP = json.load(f)
        except Exception:
            _LOCALE_MAP = {}
    return _LOCALE_MAP


def localize_text(text, lang):
    """Resolve a localization key (e.g. Custom_Slot_Armor / ui_spec_usmc_name)
    to the requested language; returns the original text when it is not a key."""
    if not isinstance(text, str) or not text.strip():
        return text
    entry = _load_locale_map().get(text.strip().lower())
    if not entry:
        return text
    return entry.get(lang) or entry.get("en") or text


# deck-slot categories ("兵种") -> localized names
CATEGORY_NAMES = {
    0: {"zh": "侦察", "ru": "Разведка", "en": "Recon"},
    1: {"zh": "步兵", "ru": "Пехота", "en": "Infantry"},
    2: {"zh": "载具", "ru": "Техника", "en": "Vehicles"},
    3: {"zh": "支援", "ru": "Поддержка", "en": "Support"},
    4: {"zh": "后勤/舰船", "ru": "Тыл/Флот", "en": "Logistics/Ship"},
    5: {"zh": "直升机", "ru": "Вертолёты", "en": "Helicopter"},
    6: {"zh": "空军", "ru": "Авиация", "en": "Air"},
}

COUNTRY_NAMES = {
    "Russia": {"zh": "俄罗斯", "ru": "Россия", "en": "Russia"},
    "USA": {"zh": "美国", "ru": "США", "en": "USA"},
    "Editor": {"zh": "编辑器", "ru": "Редактор", "en": "Editor"},
}

# Labels for the unit info block shown under the detail-window banner icon.
INFO_LABEL = {
    "armor":     {"zh": "装甲值", "en": "Armor", "ru": "Броня"},
    "health":    {"zh": "生命值", "en": "Health", "ru": "Здоровье"},
    "squad":     {"zh": "小队成员数量", "en": "Squad members", "ru": "Бойцов"},
    "vision":    {"zh": "视野", "en": "Vision", "ru": "Обзор"},
    "stealth":   {"zh": "隐蔽值", "en": "Stealth", "ru": "Скрытность"},
    "speed":     {"zh": "前进速度", "en": "Speed", "ru": "Скорость"},
    "reverse":   {"zh": "倒车速度", "en": "Reverse speed", "ru": "Скорость назад"},
    "offroad":   {"zh": "越野速度", "en": "Cross-country speed", "ru": "Скорость по бездорожью"},
    "weight":    {"zh": "重量", "en": "Weight", "ru": "Масса"},
    "cargo":     {"zh": "载重量", "en": "Cargo capacity", "ru": "Грузоподъёмность"},
    "seats":     {"zh": "座位数", "en": "Seats", "ru": "Мест"},
    "agility":   {"zh": "敏捷", "en": "Agility", "ru": "Манёвренность"},
    "turn":      {"zh": "转弯半径", "en": "Turn radius", "ru": "Радиус разворота"},
    "fuel":      {"zh": "燃油", "en": "Fuel (loiter)", "ru": "Топливо (висение)"},
    "death_time": {"zh": "阵亡回转时间", "en": "Destroyed return", "ru": "Время возврата (уничтожен)"},
    "ret_undamaged": {"zh": "毫发无损", "en": "Undamaged", "ru": "Без урона"},
    "ret_noammo":    {"zh": "弹药耗尽", "en": "Out of ammo", "ru": "Без боеприпасов"},
    "ret_death":     {"zh": "阵亡", "en": "Destroyed", "ru": "Уничтожен"},
    "kin":       {"zh": "抗穿装甲", "en": "KE armor", "ru": "Кин. защита"},
    "heat":      {"zh": "抗破装甲", "en": "CE armor", "ru": "Кум. защита"},
    "front":     {"zh": "正", "en": "front", "ru": "лоб"},
    "top":       {"zh": "顶", "en": "top", "ru": "крыша"},
    "side":      {"zh": "侧", "en": "side", "ru": "борт"},
    "rear":      {"zh": "背", "en": "rear", "ru": "корма"},
    "ability":   {"zh": "能力", "en": "Abilities", "ru": "Способности"},
    "none":      {"zh": "无", "en": "none", "ru": "нет"},
    "unit_info": {"zh": "单位信息", "en": "Unit info", "ru": "Информация о юните"},
    "role_self":   {"zh": "本体", "en": "self", "ru": "свой"},
    "role_turret": {"zh": "炮塔", "en": "turret", "ru": "башня"},
    "role_weapon": {"zh": "武器", "en": "weapon", "ru": "оружие"},
    "role_ammo":   {"zh": "弹药", "en": "ammo", "ru": "боеприпас"},
    "role_armor":  {"zh": "装甲", "en": "armor", "ru": "броня"},
    "role_sensor": {"zh": "传感器", "en": "sensor", "ru": "сенсор"},
    "role_ability":{"zh": "技能", "en": "ability", "ru": "способность"},
    "role_mobility":{"zh": "机动", "en": "mobility", "ru": "подвижность"},
    "vision_g":  {"zh": "地面视野", "en": "Ground vision", "ru": "Наземный обзор"},
    "vision_l":  {"zh": "低空视野", "en": "Low-alt vision", "ru": "Обзор на малой высоте"},
    "vision_h":  {"zh": "高空视野", "en": "High-alt vision", "ru": "Обзор на большой высоте"},
    "m":         {"zh": "m", "en": "m", "ru": "м"},
    "kmh":       {"zh": "km/h", "en": "km/h", "ru": "км/ч"},
    "kg":        {"zh": "kg", "en": "kg", "ru": "кг"},
    "s":         {"zh": "秒", "en": "s", "ru": "с"},
    # ability / capability names
    "airdrop":   {"zh": "可空投", "en": "Air-droppable", "ru": "Десантируемый"},
    "sprint":    {"zh": "可冲刺", "en": "Sprint", "ru": "Рывок"},
    "smoke":     {"zh": "烟雾弹", "en": "Smoke", "ru": "Дым"},
    "aps":       {"zh": "APS", "en": "APS", "ru": "APS"},
    "decoy":     {"zh": "诱饵", "en": "Decoy", "ru": "Ловушка"},
    "radar":     {"zh": "雷达", "en": "Radar", "ru": "Радар"},
    "laser":     {"zh": "激光照射", "en": "Laser designator", "ru": "Лазер"},
    "heal":      {"zh": "治疗", "en": "Heal", "ru": "Лечение"},
    "repair":    {"zh": "修理", "en": "Repair", "ru": "Ремонт"},
    "resupply":  {"zh": "补给", "en": "Resupply", "ru": "Пополнение"},
    "sneak":     {"zh": "潜行", "en": "Sneak", "ru": "Скрытность"},
    "afterburn": {"zh": "加力", "en": "Afterburner", "ru": "Форсаж"},
    "amphibious":{"zh": "两栖", "en": "Amphibious", "ru": "Амфибия"},
    "autofire":  {"zh": "火炮自动射击", "en": "Auto-fire artillery", "ru": "Автострельба"},
    "selfsupply":{"zh": "自我补给", "en": "Self-supply", "ru": "Самопополнение"},
}

_ICONS_DIR_LOADED = False
_ICONS_DIR = None
_ICON_INDEX = None
_ICON_CATEGORIES = ("units", "weapons", "ammo", "options", "specs",
                    "portraits", "indicators")


def _icons_dir():
    global _ICONS_DIR, _ICONS_DIR_LOADED
    if not _ICONS_DIR_LOADED:
        _ICONS_DIR_LOADED = True
        for cand in (os.path.join(_bundled_data_dir(), "icons_extracted"),
                     os.path.join(TOOL_DIR, "icons_extracted"),
                     os.path.join(os.path.dirname(sys.executable), "icons_extracted")):
            if os.path.isdir(cand):
                _ICONS_DIR = cand
                break
    return _ICONS_DIR


def _icon_index():
    global _ICON_INDEX
    if _ICON_INDEX is None:
        _ICON_INDEX = {}
        base = _icons_dir()
        if base:
            for cat in _ICON_CATEGORIES:
                d = os.path.join(base, cat)
                if not os.path.isdir(d):
                    continue
                try:
                    for fn in os.listdir(d):
                        if fn.lower().endswith(".png"):
                            _ICON_INDEX.setdefault(fn[:-4].lower(), os.path.join(d, fn))
                except Exception:
                    pass
    return _ICON_INDEX


def find_game_icon_path(name):
    """Resolve an icon asset name (e.g. RU_BTR82-Label / INF_M27) to a PNG path.

    An 'outline\\' / 'outline/' prefix marks the hollow (outline) variant;
    strip it so the solid version is used instead."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip().replace("\\", "/")
    if key.lower().startswith("outline/"):
        key = key[len("outline/"):]
    return _icon_index().get(key.lower())


_game_icon_cache = {}


def game_icon_image(tk_root, name, max_w=44, max_h=44):
    """A game icon (exported PNG) as a PhotoImage sized to fit the box.

    Uses Pillow (LANCZOS) when available for exact, smooth "vector" scaling;
    falls back to integer subsample in the packaged exe. The cache is keyed
    by (path, size) so each call site gets exactly the size it asked for."""
    path = find_game_icon_path(name)
    if not path:
        return None
    key = (path, max_w, max_h)
    if key in _game_icon_cache:
        return _game_icon_cache[key]
    try:
        import tkinter as _tk
        img = None
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            w, h = im.size
            if w > max_w or h > max_h:
                scale = min(max_w / w, max_h / h, 1.0)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               Image.LANCZOS)
            import io
            buf = io.BytesIO()
            im.save(buf, "PNG")
            img = _tk.PhotoImage(data=buf.getvalue())
        except Exception:
            img = None
        if img is None:
            img = _tk.PhotoImage(file=path)
            w, h = img.width(), img.height()
            if w > max_w or h > max_h:
                fx = max(1, -(-w // max(1, max_w)))
                fy = max(1, -(-h // max(1, max_h)))
                img = img.subsample(max(fx, fy))
            # safety: keep halving until it fits the box (packaged exe)
            while img.width() > max_w * 2 or img.height() > max_h * 2:
                img = img.subsample(2)
        _game_icon_cache[key] = img
        return img
    except Exception:
        _game_icon_cache[key] = None
        return None


def pick_cjk_font(families):
    """Choose the requested CJK font (新宋体 / NSimSun) from available families."""
    for fam in ("新宋体", "NSimSun", "SimSun", "Microsoft YaHei", "微软雅黑"):
        if fam in families:
            return fam
    return None


_CJK_FAMILY = ""


def cjk_family():
    """The resolved CJK font family ("" = platform default)."""
    return _CJK_FAMILY


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class ToolTip:
    """Small delayed hover tooltip (brief field/table meaning)."""

    def __init__(self, widget, text_getter, colors=None):
        import tkinter as tk
        self.tk = tk
        self.widget = widget
        self.text_getter = text_getter
        self.colors = colors or {}
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(400, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        self._after = None
        text = self.text_getter()
        if not text:
            return
        if self.tip is not None:
            return
        self.tip = self.tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass
        c = self.colors
        fam = cjk_family()
        wrap = self.tk.Label(self.tip, justify="left", wraplength=460,
                             bg=c.get("list_bg", "#ffffff"),
                             relief="solid", borderwidth=1, padx=7, pady=4)
        wrap.pack()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i == 0:
                f = (fam, 11, "bold")
                fg = c.get("label_key", "#33507a")
            else:
                f = (fam, 11)
                fg = c.get("list_fg", "#000000")
            row = self.tk.Label(wrap, text=line, justify="left", anchor="w",
                                bg=c.get("list_bg", "#ffffff"), fg=fg, font=f)
            row.pack(fill="x", padx=1, pady=0)
        x = self.widget.winfo_pointerx() + 16
        y = self.widget.winfo_pointery() + 14
        self.tip.wm_geometry("+%d+%d" % (x, y))

    def _hide(self, _event=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "settings.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# cross-table reference mapping
# ---------------------------------------------------------------------------

SINGULAR = {
    "Units": "Unit", "Weapons": "Weapon", "Abilities": "Ability",
    "Ammunitions": "Ammunition", "Armors": "Armor", "Sensors": "Sensor",
    "Turrets": "Turret", "Modifications": "Modification", "Options": "Option",
    "Mobility": "Mobility", "FlyPresets": "FlyPreset",
    "Specializations": "Specialization", "Countries": "Country",
    "SquadMembers": "SquadMember", "SquadWeapons": "SquadWeapon",
    "TurretUnits": "TurretUnit", "TurretWeapons": "TurretWeapon",
    "WeaponAmmunitions": "WeaponAmmunition", "UnitAbilities": "UnitAbility",
    "UnitArmors": "UnitArmor", "UnitPropulsions": "UnitPropulsion",
    "SensorUnits": "SensorUnit", "TransportAvailabilities": "TransportAvailability",
    "SpecializationAvailabilities": "SpecializationAvailability",
}


def build_field_ref_map():
    m = {}
    for table, sing in SINGULAR.items():
        m[sing + "Id"] = table
    m.update({
        "ReplaceUnitId": "Units", "MainSensorId": "Sensors", "ExtraSensorId": "Sensors",
        "PrimaryWeaponId": "Weapons", "SecondaryWeaponId": "Weapons",
        "SpecialWeaponId": "Weapons", "BaseWeaponId": "Weapons", "TargetWeaponId": "Weapons",
        "Turret0Id": "Turrets", "Turret1Id": "Turrets", "Turret2Id": "Turrets",
        "Turret3Id": "Turrets", "Turret4Id": "Turrets", "Turret5Id": "Turrets",
        "Turret6Id": "Turrets", "Turret7Id": "Turrets", "Turret8Id": "Turrets",
        "Turret9Id": "Turrets", "Turret10Id": "Turrets", "Turret11Id": "Turrets",
        "Turret12Id": "Turrets", "Turret13Id": "Turrets", "Turret14Id": "Turrets",
        "Turret15Id": "Turrets", "Turret16Id": "Turrets", "Turret17Id": "Turrets",
        "Turret18Id": "Turrets", "Turret19Id": "Turrets", "Turret20Id": "Turrets",
        "Ability1Id": "Abilities", "Ability2Id": "Abilities", "Ability3Id": "Abilities",
        "FlyPresetId": "FlyPresets",
    })
    return m


FIELD_REF_MAP = build_field_ref_map()

# Browsing view: only these key columns are shown per table (the full row is
# always available in the field editor). FK columns (UnitId, WeaponId, ...)
# are resolved to the referenced row's name automatically.
KEY_COLUMNS = {
    # v1.8.55：单位大类的三个"分类"字段并排显示（Type / CategoryType / Role），
    # 单元格里带绿色箭头指向它 ID 对应的种类（见 _enum_cell）。
    "Units": ["Id", "HUDName", "Name", "CountryId", "Cost", "Type", "CategoryType", "Role"],
    "Weapons": ["Id", "Name", "HUDName"],
    "Abilities": ["Id", "Name", "IsDefault"],
    "Ammunitions": ["Id", "Name", "HUDName"],
    "Armors": ["Id", "Name", "IsDefault"],
    "Sensors": ["Id", "Name", "IsDefault"],
    "Mobility": ["Id", "Name", "IsDefault"],
    "FlyPresets": ["Id", "Name"],
    "Turrets": ["Id", "Name", "ModelFileName"],
    "Countries": ["Id", "Name", "UIName", "Hidden"],
    "Modifications": ["Id", "Name", "UnitId", "Type", "UIName"],
    "Options": ["Id", "Name", "ModificationId", "IsDefault", "Cost", "ReplaceUnitId"],
    "Specializations": ["Id", "CountryId", "Name", "UIName"],
    "UnitAbilities": ["Id", "UnitId", "AbilityId"],
    "TurretUnits": ["Id", "UnitId", "TurretId", "Order"],
    "TurretWeapons": ["Id", "TurretId", "WeaponId", "Order"],
    "SquadMembers": ["Id", "UnitId", "ModelFileName", "PrimaryWeaponId", "SpecialWeaponId"],
    "SquadWeapons": ["Id", "UnitId", "WeaponId", "Order"],
    "WeaponAmmunitions": ["Id", "UnitId", "WeaponId", "AmmunitionId", "Quantity"],
    "UnitArmors": ["Id", "UnitId", "ArmorId"],
    "UnitPropulsions": ["Id", "UnitId", "MobilityId"],
    "SensorUnits": ["Id", "UnitId", "SensorId"],
    "SpecializationAvailabilities": ["Id", "SpecializationId", "UnitId"],
    "TransportAvailabilities": ["Id", "SpecializationAvailabilityId", "UnitId"],
}

# Fields matched by the global "Find Id" search box (toolbar + detail editor).
# Covers the human-readable names plus the UI-art fields modders look up when
# they add new option pictures, unit / slot thumbnails or HUD icons.
LOOKUP_FIELDS = ("Name", "HUDName", "UIName", "OptionPicture",
                 "ThumbnailFileName", "ThumbnailOverride", "HUDIcon")

# Which link table backs each relation-group fold bar, for "delete whole group".
GROUP_LINK_TABLES = {
    "abilities": "UnitAbilities",
    "turrets": "TurretUnits",
    "squad_weapons": "SquadWeapons",
    "squad_members": "SquadMembers",
    "ammo": "WeaponAmmunitions",
    "armors": "UnitArmors",
    "mobility": "UnitPropulsions",
    "sensors": "SensorUnits",
    "mods": "Modifications",
}


def collect_ids(tables):
    out = {}
    for name, rows in tables.items():
        ids = set()
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("Id"), int):
                ids.add(r["Id"])
        out[name] = ids
    return out


def find_references(tables, table, row_id):
    results = []
    for tname, rows in tables.items():
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            for field, val in r.items():
                if field == "Id" or not isinstance(val, int):
                    continue
                if FIELD_REF_MAP.get(field) == table and val == row_id:
                    results.append((tname, i, field))
    return results



# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_db(tables, baseline_sigs=None):
    """Validate. If baseline_sigs (set of issue signatures) is given, only
    issues NOT present in the baseline are reported (clean-DB diff)."""
    issues = []
    ids_by_table = collect_ids(tables)
    units = tables.get("Units", [])

    for tname, rows in tables.items():
        seen = {}
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                issues.append({"table": tname, "idx": i, "kind": "no_id", "id": None,
                               "text": f"{tname} row {i}: not an object"})
                continue
            rid = r.get("Id")
            if not isinstance(rid, int):
                issues.append({"table": tname, "idx": i, "kind": "no_id", "id": None,
                               "text": f"{tname} row {i}: missing/invalid Id"})
                continue
            seen.setdefault(rid, []).append(i)
        for rid, idxs in seen.items():
            if len(idxs) > 1:
                issues.append({"table": tname, "idx": idxs[0], "kind": "dup_id", "id": rid,
                               "rows": idxs,
                               "text": f"duplicate Id {rid}: {tname} rows {idxs}"})

    prop_ids = set()
    for r in tables.get("UnitPropulsions", []):
        if isinstance(r, dict) and isinstance(r.get("UnitId"), int):
            prop_ids.add(r["UnitId"])
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            continue
        uid = u.get("Id")
        if isinstance(uid, int) and uid not in prop_ids:
            issues.append({"table": "Units", "idx": i, "kind": "no_propulsion", "id": uid,
                           "name": u.get("Name", ""),
                           "text": f"Units.Id={uid} {u.get('Name', '')}: no UnitPropulsions row"})

    ammo_keys = set()
    for r in tables.get("WeaponAmmunitions", []):
        if isinstance(r, dict) and isinstance(r.get("UnitId"), int) and isinstance(r.get("WeaponId"), int):
            ammo_keys.add((r["UnitId"], r["WeaponId"]))
    turret_by_unit = {}
    for r in tables.get("TurretUnits", []):
        if isinstance(r, dict) and isinstance(r.get("UnitId"), int) and isinstance(r.get("TurretId"), int):
            turret_by_unit.setdefault(r["UnitId"], set()).add(r["TurretId"])
    weapons_by_turret = {}
    for r in tables.get("TurretWeapons", []):
        if isinstance(r, dict) and isinstance(r.get("TurretId"), int) and isinstance(r.get("WeaponId"), int):
            weapons_by_turret.setdefault(r["TurretId"], set()).add(r["WeaponId"])
    squad_weapons_by_unit = {}
    for r in tables.get("SquadWeapons", []):
        if isinstance(r, dict) and isinstance(r.get("UnitId"), int) and isinstance(r.get("WeaponId"), int):
            squad_weapons_by_unit.setdefault(r["UnitId"], set()).add(r["WeaponId"])
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            continue
        uid = u.get("Id")
        if not isinstance(uid, int):
            continue
        wset = set(squad_weapons_by_unit.get(uid, set()))
        for t in turret_by_unit.get(uid, set()):
            wset |= weapons_by_turret.get(t, set())
        for wid in sorted(wset):
            if (uid, wid) not in ammo_keys:
                issues.append({"table": "Units", "idx": i, "kind": "weapon_no_ammo", "id": uid,
                               "subid": wid, "name": u.get("Name", ""),
                               "text": f"Unit.Id={uid} {u.get('Name','')} Weapon.Id={wid}: no WeaponAmmunitions row"})

    armor_is_default = {}
    for r in tables.get("Armors", []):
        if isinstance(r, dict) and isinstance(r.get("Id"), int):
            armor_is_default[r["Id"]] = bool(r.get("IsDefault"))
    unit_armors = {}
    for r in tables.get("UnitArmors", []):
        if isinstance(r, dict) and isinstance(r.get("UnitId"), int) and isinstance(r.get("ArmorId"), int):
            unit_armors.setdefault(r["UnitId"], []).append(r["ArmorId"])
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            continue
        uid = u.get("Id")
        armors = unit_armors.get(uid, [])
        if armors and not any(armor_is_default.get(a, False) for a in armors):
            issues.append({"table": "Units", "idx": i, "kind": "armor_no_default", "id": uid,
                           "name": u.get("Name", ""),
                           "text": f"Unit.Id={uid} {u.get('Name','')}: no IsDefault armor"})

    for tname, rows in tables.items():
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            for field, val in r.items():
                if not isinstance(val, int) or val <= 0 or field == "Id":
                    continue
                target = FIELD_REF_MAP.get(field)
                if target and target in ids_by_table and val not in ids_by_table[target]:
                    issues.append({"table": tname, "idx": i, "kind": "bad_ref", "id": val,
                                   "field": field,
                                   "text": f"{tname}.{field}={val} (row {i}): missing {target} Id"})

    if baseline_sigs is not None:
        issues = [it for it in issues
                  if issue_sig(it) not in baseline_sigs]
    return issues


def issue_text(it, t):
    """Human-readable validation message in the requested UI language
    (t is a Translator or any object with .t(key, **kwargs))."""
    kind = it.get("kind")
    table = it.get("table", "")
    idx = it.get("idx")
    rid = it.get("id")
    name = it.get("name", "")
    if kind == "no_id":
        return t.t("validate_no_id", table=table, idx=idx)
    if kind == "dup_id":
        rows = it.get("rows") or []
        return t.t("validate_dup_id", id=rid, table=table,
                   rows=", ".join(str(x) for x in rows))
    if kind == "no_propulsion":
        return t.t("validate_no_propulsion", id=rid, name=name)
    if kind == "weapon_no_ammo":
        return t.t("validate_weapon_no_ammo", id=rid, name=name, wid=it.get("subid"))
    if kind == "armor_no_default":
        return t.t("validate_armor_no_default", id=rid, name=name)
    if kind == "bad_ref":
        return t.t("validate_bad_ref", table=table, field=it.get("field", ""),
                   value=rid, idx=idx)
    return it.get("text", "")


# ---------------------------------------------------------------------------
# undo / redo (per-table snapshots)
# ---------------------------------------------------------------------------

class UndoStack:
    """Snapshot-based undo/redo.

    Each entry records a whole table's before/after state (JSON), so undoing a
    row add/delete/duplicate/field-edit restores the entire table. Limited to
    MAX_UNDO entries; pushing past that drops the oldest.
    """

    def __init__(self):
        self.items = []
        self.pos = -1

    def push(self, table, old_value, new_value, label=""):
        self.items = self.items[:self.pos + 1]
        self.items.append({
            "table": table,
            "old": json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
            "new": json.dumps(new_value, ensure_ascii=False),
            "label": label,
        })
        if len(self.items) > MAX_UNDO:
            self.items.pop(0)
        self.pos = len(self.items) - 1

    def can_undo(self):
        return self.pos >= 0

    def can_redo(self):
        return self.pos < len(self.items) - 1

    def undo(self, tables):
        if not self.can_undo():
            return None
        item = self.items[self.pos]
        tables[item["table"]] = json.loads(item["old"]) if item["old"] is not None else []
        self.pos -= 1
        return item["table"]

    def redo(self, tables):
        if not self.can_redo():
            return None
        item = self.items[self.pos + 1]
        tables[item["table"]] = json.loads(item["new"])
        self.pos += 1
        return item["table"]

    def clear(self):
        self.items = []
        self.pos = -1


def fmt_cell(value, width=120):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\r", " ").replace("\n", " ")
    return s[:width]


def next_free_id(rows):
    mx = 0
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("Id"), int):
            mx = max(mx, r["Id"])
    return mx + 1


BASELINE_PATH = os.path.join(_bundled_data_dir(), "clean_baseline.json")


def issue_sig(it):
    """Stable identity of an issue for baseline comparison."""
    return (it["kind"], it["table"], it["id"], it.get("subid"))


def load_baseline():
    """Return the bundled clean-DB tables, or None."""
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    return None


_BASELINE_SIGS_CACHE = None


def baseline_issue_sigs(force=False):
    """Set of issue signatures for the bundled clean DB's own issues."""
    global _BASELINE_SIGS_CACHE
    if _BASELINE_SIGS_CACHE is not None and not force:
        return _BASELINE_SIGS_CACHE
    base = load_baseline()
    if not base:
        _BASELINE_SIGS_CACHE = set()
    else:
        _BASELINE_SIGS_CACHE = {issue_sig(it)
                                for it in validate_db(base, None)}
    return _BASELINE_SIGS_CACHE



# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class LookupBox:
    """Inline name-lookup entry with an auto-dropdown of matches.

    Type a name / HUD name / UIName / OptionPicture / Thumbnail / HUDIcon / Id;
    matches pop up below the entry.
    Enter or click opens the item; Down/Up move; Esc hides.
    Reused by the main window toolbar and the detail editor window.
    """

    def __init__(self, app, parent, width=26, owner=None):
        self.app = app
        self.owner = owner
        self.var = app.tk.StringVar()
        self.entry = app.tk.Entry(parent, textvariable=self.var, width=width)
        self.entry.bind("<KeyRelease>", self._debounce)
        self.entry.bind("<Return>", self._open_first)
        self.entry.bind("<Down>", self._move)
        self.entry.bind("<Up>", self._move)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<FocusOut>", self._focus_out)
        self._after = None
        self._win = None
        self._lb = None
        self._results = []
        self._sel = 0

    def focus(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self._update()

    def hide(self):
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win = None
        self._lb = None

    def _debounce(self, _event=None):
        if self._after is not None:
            self.app.root.after_cancel(self._after)
        self._after = self.app.root.after(200, self._update)

    def _focus_out(self, _event=None):
        self.app.root.after(250, self._check_focus)

    def _check_focus(self):
        try:
            f = self.app.root.focus_get()
        except Exception:
            f = None
        if f is self.entry:
            return
        if self._lb is not None and f is self._lb:
            return
        self.hide()

    def _update(self):
        q = self.var.get().strip()
        if not q:
            self.hide()
            return
        results = self.app.search_names(q)[:30]
        self._results = results
        self._sel = 0
        if not results:
            self.hide()
            return
        if self._win is None:
            self._win = self.app.tk.Toplevel(self.app.root)
            self._win.overrideredirect(True)
            self._lb = self.app.tk.Listbox(self._win, width=70, height=min(len(results), 12))
            self._lb.pack(fill="both", expand=True)
            self._lb.bind("<Double-1>", self._open_selected)
            self._lb.bind("<Return>", self._open_selected)
            self._lb.bind("<ButtonRelease-1>", self._open_selected)
            if hasattr(self.app, "bind_wheel"):
                self.app.bind_wheel(self._lb)
            if hasattr(self.app, "theme_children"):
                self.app.theme_children(self._win)
        lb = self._lb
        lb.delete(0, "end")
        for (tname, i, rid, name, hud, extras) in results:
            line = f"{tname}  |  Id={rid}  |  {name}"
            if hud and hud != name:
                line += f"  |  HUD: {hud}"
            if extras:
                line += "  |  " + "  |  ".join(extras)
            lb.insert("end", line)
        if len(results) >= 2:
            lb.selection_set(0)
        self._place_dropdown()

    def _place_dropdown(self):
        """Position the popup below the entry, clamping it to the screen so it
        never spills off the edges (e.g. when the window is maximized)."""
        if self._win is None:
            return
        try:
            self._win.update_idletasks()
            sw = self._win.winfo_screenwidth()
            sh = self._win.winfo_screenheight()
            w = self._win.winfo_reqwidth()
            h = self._win.winfo_reqheight()
        except Exception:
            return
        ex = self.entry.winfo_rootx()
        ey = self.entry.winfo_rooty()
        eh = self.entry.winfo_height()

        # horizontal: left-align with the entry; shift left if it overflows
        x = ex
        if x + w > sw:
            x = max(0, ex + self.entry.winfo_width() - w)

        # vertical: below the entry when it fits, otherwise above
        y = ey + eh + 2
        if y + h > sh:
            y = ey - h - 2
            if y < 0:
                y = 0

        self._win.geometry(f"+{x}+{y}")

    def _move(self, event):
        if self._win is None or not self._results:
            return
        n = len(self._results)
        if event.keysym == "Down":
            self._sel = min(self._sel + 1, n - 1)
        else:
            self._sel = max(self._sel - 1, 0)
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)
        return "break"

    def _open_selected(self, _event=None):
        if not self._results:
            return
        if self._lb is not None:
            sel = self._lb.curselection()
            if sel:
                self._sel = sel[0]
        if 0 <= self._sel < len(self._results):
            tname, i, _rid, _n, _h, _x = self._results[self._sel]
            self.hide()
            if self.owner is not None:
                # navigate inside the owning detail window so its Back history
                # keeps working (no new window)
                self.owner.load(tname, i, push=True)
            else:
                self.app._jump_to(tname, i)

    def _open_first(self, _event=None):
        if self._win is None:
            self._update()
            if self._win is None:
                return "break"
        self._open_selected()
        return "break"

# -*- coding: utf-8 -*-
"""Detail window: full row editor + relations, opened by double-click."""


class DetailWindow:
    """Modeless drill-down editor window.

    Left pane: every field of the current row, directly editable.
    Right pane: relations of the row (for units: abilities / turrets / weapons /
    ammo / armors / mobility / modifications; for other tables: in/out refs).
    Single-click a relation item shows the related entity read-only on the
    left (the relations tree stays on the current row), and the inline mini
    editor below the tree shows the link row; Delete removes it.
    """

    def __init__(self, app):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.table = None
        self.row = None
        self.row_id = None
        self.history = []
        self._widgets = {}
        self._pending = False
        self.rel_map = {}
        self.mini_state = None
        self.preview = None
        self.mini_widgets = {}
        self.mini_pending = False
        self._auto_after = None

        self.win = tk.Toplevel(app.root)
        self.win.title(app_title_text(app.tr))
        self.win.geometry("1120x820")
        self.win.minsize(860, 600)
        # mirror the main window's maximized state so a double-clicked editor
        # opens maximized when the main window is maximized
        try:
            if app.root.state() == "zoomed":
                self.win.state("zoomed")
        except Exception:
            pass

        top = tk.Frame(self.win, padx=4, pady=3,
                        highlightbackground=app.c["border"], highlightthickness=1)
        top.pack(side="top", fill="x")
        # large banner: big icon box + the row's name (单位/武器/弹药等详情)
        self.banner_icon_l = tk.Label(top, bg=app.c["bg"],
                                      relief="solid", borderwidth=1,
                                      highlightbackground=app.c["border"])
        self.banner_icon_l.pack(side="left", padx=(10, 6), pady=2)
        self.banner_name_l = tk.Label(top, anchor="w", font=(cjk_family(), 13, "bold"),
                                      fg=app.c["fg"], bg=app.c["bg"])
        self.banner_name_l.pack(side="left", padx=(0, 10))
        self._banner_icon_ref = None
        self.title_l = tk.Label(top, anchor="w", font=(cjk_family(), 10, "bold"))
        self.title_l._color_role = "label_key"
        self.title_l.pack(side="left", padx=4)
        self.lookup_label_w = tk.Label(top)
        self.lookup_label_w.pack(side="left", padx=(14, 2))
        self.lookup_box = LookupBox(app, top, width=22, owner=self)
        self.lookup_box.entry.pack(side="left", padx=2)
        self.dict_btn = tk.Button(top, command=self.app.show_dictionary, padx=8)
        self.dict_btn.pack(side="right", padx=3)
        self.back_btn = tk.Button(top, command=self.back, padx=8)
        self.back_btn.pack(side="right", padx=3)
        self.dup_btn = tk.Button(top, command=self.duplicate_current, padx=8)
        self.dup_btn.pack(side="right", padx=3)
        self.clone_btn = tk.Button(top, command=self.clone_current, padx=8)
        self.clone_btn.pack(side="right", padx=3)
        self.hint_l = tk.Label(self.win, anchor="w")
        self.hint_l._color_role = "label_key"
        self.hint_l.pack(side="bottom", fill="x", padx=6, pady=(0, 2))

        # unit stat info block (collapsible fold bar) below the banner icon
        self._info_open = False
        self.info_wrap = tk.Frame(self.win, bg=app.c["entry_bg"],
                                  highlightbackground=app.c["border"],
                                  highlightthickness=1)
        self.info_wrap.pack(side="top", fill="x", padx=4, pady=(0, 2))
        self.info_header = tk.Label(self.info_wrap, anchor="w",
                                    font=(cjk_family(), 11, "bold"),
                                    fg=app.c["accent"], bg=app.c["entry_bg"],
                                    padx=6, pady=2, cursor="hand2")
        self.info_header.pack(fill="x")
        self.info_header.bind("<Button-1>", self._toggle_info)
        self.info_body = tk.Frame(self.info_wrap, bg=app.c["entry_bg"])

        paned = tk.PanedWindow(self.win, orient="horizontal", sashwidth=5)
        paned.pack(fill="both", expand=True, padx=4, pady=4)
        self.main_paned = paned

        left = tk.Frame(paned)
        self.fields_canvas = tk.Canvas(left, highlightthickness=0, bg=app.c["bg"])
        self.fields_vsb = ttk.Scrollbar(left, orient="vertical", command=self.fields_canvas.yview)
        self.fields_inner = tk.Frame(self.fields_canvas, bg=app.c["bg"])
        self.fields_inner.bind("<Configure>",
                               lambda e: self.fields_canvas.configure(scrollregion=self.fields_canvas.bbox("all")))
        fwin = self.fields_canvas.create_window((0, 0), window=self.fields_inner, anchor="nw")
        self.fields_canvas.bind("<Configure>", lambda e: self.fields_canvas.itemconfig(fwin, width=e.width))
        self.fields_canvas.configure(yscrollcommand=self.fields_vsb.set)
        self.fields_canvas.pack(side="left", fill="both", expand=True)
        self.fields_vsb.pack(side="right", fill="y")
        app.bind_wheel(self.fields_canvas)
        paned.add(left, minsize=380, stretch="always")

        right = tk.PanedWindow(paned, orient="vertical", sashwidth=5)
        paned.add(right, minsize=360, stretch="always")

        rtop = tk.Frame(right)
        rel_head = tk.Frame(rtop)
        self.rel_label_l = tk.Label(rel_head, anchor="w", font=(cjk_family(), 10, "bold"))
        self.rel_label_l.pack(side="left", padx=6)
        self.rel_dup_btn = tk.Button(rel_head, command=self.on_rel_duplicate, padx=6)
        self.rel_dup_btn.pack(side="right", padx=6)
        rel_head.pack(fill="x")
        self.rel_tree = ttk.Treeview(rtop, show="tree", selectmode="browse", columns=(), style="Rel.Treeview")
        self.rel_vsb = ttk.Scrollbar(rtop, orient="vertical", command=self.rel_tree.yview)
        self.rel_tree.configure(yscrollcommand=self.rel_vsb.set)
        self.rel_tree.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.rel_vsb.pack(side="right", fill="y")
        self.rel_tree.bind("<<TreeviewSelect>>", self.on_rel_select)
        self.rel_tree.bind("<Delete>", self.on_rel_delete)
        self.rel_tree.bind("<Button-1>", self._on_rel_click)
        self.rel_tree.bind("<Button-3>", self.on_rel_context)
        self._rel_tip = None
        app.bind_wheel(self.rel_tree)
        right.add(rtop, minsize=160, stretch="always")

        mbottom = tk.Frame(right)
        self.mini_label_l = tk.Label(mbottom, anchor="w", font=(cjk_family(), 9, "bold"), fg="#33507a")
        self.mini_label_l.pack(fill="x", padx=6, pady=(2, 0))
        self.mini_canvas = tk.Canvas(mbottom, highlightthickness=0, bg=app.c["bg"])
        self.mini_vsb = ttk.Scrollbar(mbottom, orient="vertical", command=self.mini_canvas.yview)
        self.mini_inner = tk.Frame(self.mini_canvas, bg=app.c["bg"])
        self.mini_inner.bind("<Configure>",
                             lambda e: self.mini_canvas.configure(scrollregion=self.mini_canvas.bbox("all")))
        mwin = self.mini_canvas.create_window((0, 0), window=self.mini_inner, anchor="nw")
        self.mini_canvas.bind("<Configure>", lambda e: self.mini_canvas.itemconfig(mwin, width=e.width))
        self.mini_canvas.configure(yscrollcommand=self.mini_vsb.set)
        self.mini_canvas.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.mini_vsb.pack(side="right", fill="y")
        app.bind_wheel(self.mini_canvas)
        right.add(mbottom, minsize=120, stretch="always")

        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<F3>", lambda e: self.lookup_box.focus())
        self.win.bind("<Control-d>", self.on_rel_duplicate)
        self.win.bind("<Control-D>", self.on_rel_duplicate)
        self.win.bind("<Control-z>", lambda e: self.app.do_undo())
        self.win.bind("<Control-y>", lambda e: self.app.do_redo())
        self.win.bind("<Control-Y>", lambda e: self.app.do_redo())
        self.apply_lang()
        app.theme_children(self.win)
        app.theme_children(self.fields_inner)
        app.theme_children(self.mini_inner)
        self.win.after(200, lambda: app._set_title_bar(self.win, True))
        self.win.bind("<Map>", lambda e: app._set_title_bar(self.win, True))

    def apply_lang(self):
        tr = self.app.tr
        app = self.app
        self.app._icon_text(self.dict_btn, "dict", tr.t("btn_dict"))
        self.app._icon_text(self.back_btn, "back", tr.t("dlg_back"))
        self.app._icon_text(self.lookup_label_w, "lookup", tr.t("btn_lookup") + ":")
        self.app._icon_text(self.dup_btn, "dup", tr.t("dlg_duplicate"))
        self.app._icon_text(self.clone_btn, "clone", tr.t("dlg_clone"))
        self.app._icon_text(self.rel_label_l, "refs", tr.t("rel_tab_relations"))
        self.app._icon_text(self.rel_dup_btn, "dup", tr.t("rel_duplicate"))
        self.mini_label_l.config(text=tr.t("rel_mini_hint"))
        self.hint_l.config(text=tr.t("rel_jump_hint"))
        if self.table is not None:
            self.commit(silent=True)
            self.rebuild()

    def _info_title(self):
        return INFO_LABEL["unit_info"].get(self.app.tr.lang,
                                           INFO_LABEL["unit_info"]["en"])

    def _toggle_info(self, _event=None):
        self._info_open = not self._info_open
        self._apply_info_visibility()

    def _apply_info_visibility(self):
        if not hasattr(self, "info_header"):
            return
        mark = FOLD_OPEN if self._info_open else FOLD_CLOSED
        self.info_header.config(text=mark + self._info_title())
        if self._info_open:
            self.info_body.pack(fill="x")
        else:
            self.info_body.pack_forget()

    def _refresh_info(self):
        """Rebuild the per-category stat block under the banner icon."""
        if not hasattr(self, "info_body"):
            return
        for w in self.info_body.winfo_children():
            w.destroy()
        vt, vi = self._view()
        uid = None
        if vt == "Units" and vi is not None:
            units = self.app.tables.get("Units")
            if units is not None and 0 <= vi < len(units) and isinstance(units[vi], dict):
                uid = units[vi].get("Id")
        if not isinstance(uid, int):
            self.info_wrap.pack_forget()
            return
        if not self.info_wrap.winfo_ismapped():
            self.info_wrap.pack(side="top", fill="x", padx=4, pady=(0, 2),
                                before=self.main_paned)
        lines = self.app._unit_info_lines(uid)
        c = self.app.c
        fam = cjk_family()
        for text, tip in lines:
            lab = self.app.tk.Label(self.info_body, text=text, anchor="w",
                                    justify="left", font=(fam, 11),
                                    fg=c["hint"], bg=c["entry_bg"], padx=4)
            lab.pack(anchor="w", fill="x")
            if tip:
                ToolTip(lab, lambda tip=tip: tip, colors=c)
        self._apply_info_visibility()

    def load(self, table, idx, push=True):
        if self.table is not None:
            if not self.commit(silent=False) or not self.commit_mini(silent=False):
                return False
            if push:
                self.history.append((self.table, self.row))
        self.table = table
        self.row = idx
        self.row_id = None
        self.rebuild()
        self.win.deiconify()
        self.win.lift()
        return True

    def back(self):
        if not self.history:
            return
        if self.table is not None and (not self.commit(silent=False) or not self.commit_mini(silent=False)):
            return
        self.table, self.row = self.history.pop()
        self.row_id = None
        self.rebuild()

    def _clear_fields(self, canvas_inner):
        self._fk_hide()
        for w in canvas_inner.winfo_children():
            w.destroy()

    def _dirty(self, *_args):
        self._pending = True
        self._schedule_auto_commit()

    def _mini_dirty(self, *_args):
        self.mini_pending = True
        self._schedule_auto_commit()

    def _schedule_auto_commit(self):
        """Auto-apply edits a short while after the user stops typing."""
        if self._auto_after is not None:
            self.app.root.after_cancel(self._auto_after)
        self._auto_after = self.app.root.after(600, self._auto_commit)

    def _auto_commit(self):
        self._auto_after = None
        if self._pending:
            self.commit(silent=True)
        if self.mini_pending:
            self.commit_mini(silent=True)

    def _on_field_focus_out(self, _event=None):
        """A field lost focus: close the FK dropdown (if any), then apply the
        edits immediately."""
        app = self.app
        app.root.after(250, self._fk_check_focus)
        app.root.after(300, self._commit_after_focus_out)

    def _commit_after_focus_out(self):
        if self.app._fk_win is not None:
            return  # the user is still picking from the FK dropdown
        if self._pending:
            self.commit(silent=True)
        if self.mini_pending:
            self.commit_mini(silent=True)

    # ---------------- smart FK fields (type a name -> Id) ----------------

    def _fk_update_label(self, entry):
        """Show the referenced name next to a numeric FK value."""
        app = self.app
        name_l = getattr(entry, "_fk_name_label", None)
        target = getattr(entry, "_fk_target", None)
        if name_l is None or target is None:
            return
        t = entry.get().strip()
        if not t.lstrip("-").isdigit():
            name_l.config(text="", fg=app.c["hint"])
            return
        try:
            rid = int(t)
        except ValueError:
            name_l.config(text="", fg=app.c["hint"])
            return
        if rid <= 0:
            name_l.config(text="→ 0", fg=app.c["hint"])
            name_l._color_role = "hint"
            return
        app._ensure_indexes()
        if rid in app._id_index.get(target, {}):
            field = getattr(entry, "_fk_field", None)
            name_l.config(text=f"→ {app._fk_name_for(field, target, rid)}",
                          fg=app.c["ok"])
            name_l._color_role = "ok"
        else:
            name_l.config(text=f"→ ⚠ {app.tr.t('lookup_none')}", fg=app.c["warn"])
            name_l._color_role = "warn"

    def _enum_update_label(self, entry, name_l, ekey, table=None):
        """v1.8.55：枚举字段右侧的绿色箭头标签。

        值合法 → `➜ 主战坦克 (Tank)`（绿色）；值不在枚举里 → `➜ ⚠ 未知值 99`
        （黄色，游戏会走兜底分支，不会崩）；空/非数字 → 提示（灰色）。"""
        app = self.app
        t = entry.get().strip()
        if not t:
            name_l.config(text="➜", fg=app.c["hint"])
            name_l._color_role = "hint"
            return
        try:
            val = int(t)
        except ValueError:
            name_l.config(text="➜ ⚠ " + app.tr.t("enum_invalid"), fg=app.c["warn"])
            name_l._color_role = "warn"
            return
        label = enum_label(ekey, val, app.tr.lang)
        if label:
            name_l.config(text="➜ " + label, fg=app.c["ok"])
            name_l._color_role = "ok"
        else:
            name_l.config(text="➜ ⚠ " + app.tr.t("enum_unknown", value=val),
                          fg=app.c["warn"])
            name_l._color_role = "warn"

    def _enum_tip_text(self, ekey):
        """枚举字段的悬浮提示：真实作用说明 + 全部取值对照（点击可打开词典）。"""
        lang = self.app.tr.lang
        parts = [ekey]
        note = enum_note(ekey, lang)
        if note:
            parts.append(note)
        parts.append("")
        for val, meaning in enum_values(ekey, lang):
            member = enum_member(ekey, val)
            parts.append("  %s = %s%s" % (val, meaning, ("  [%s]" % member) if member else ""))
        parts.append("")
        parts.append(self.app.tr.t("enum_click_hint"))
        return "\n".join(parts)

    def _fk_schedule(self, entry, target):
        app = self.app
        if app._fk_after is not None:
            app.root.after_cancel(app._fk_after)
        app._fk_after = app.root.after(200, lambda: self._fk_show(entry, target))

    def _fk_show(self, entry, target):
        app = self.app
        q = entry.get().strip()
        if not q or q.lstrip("-").isdigit():
            self._fk_hide()
            return
        results = []
        for i, r in enumerate(app.tables.get(target, [])):
            if not isinstance(r, dict) or not isinstance(r.get("Id"), int):
                continue
            disp = ""
            try:
                disp = app._display_name(target, r) or ""
            except Exception:
                disp = ""
            names = [r[k] for k in ("Name", "HUDName", "UIName")
                     if isinstance(r.get(k), str)]
            if disp and disp not in names:
                names.append(disp)
            if any(q.lower() in n.lower() for n in names):
                name = r.get("Name") or r.get("HUDName") or ""
                hud = r.get("HUDName") or ""
                results.append((i, r["Id"], name or disp, hud))
                if len(results) >= 20:
                    break
        app._fk_results = results
        if not results:
            self._fk_hide()
            return
        if app._fk_win is None:
            app._fk_win = app.tk.Toplevel(app.root)
            app._fk_win.overrideredirect(True)
            app._fk_lb = app.tk.Listbox(app._fk_win, width=64, height=min(len(results), 8))
            app._fk_lb.pack(fill="both", expand=True)
            app._fk_lb.bind("<ButtonRelease-1>", lambda e: self._fk_select())
            app._fk_lb.bind("<Double-1>", lambda e: self._fk_select())
            app._fk_lb.bind("<Return>", lambda e: self._fk_select())
            app.bind_wheel(app._fk_lb)
            app.theme_children(app._fk_win)
        lb = app._fk_lb
        lb.delete(0, "end")
        for (_i, rid, name, hud) in results:
            line = f"Id={rid}  |  {name}"
            if hud and hud != name:
                line += f"  |  HUD: {hud}"
            lb.insert("end", line)
        app._fk_entry = entry
        x = entry.winfo_rootx()
        y = entry.winfo_rooty() + entry.winfo_height() + 2
        app._fk_win.geometry(f"+{x}+{y}")

    def _fk_select(self, _event=None):
        app = self.app
        if not app._fk_results or app._fk_entry is None:
            return
        lb = app._fk_lb
        sel = lb.curselection() if lb is not None else ()
        idx = sel[0] if sel else 0
        if 0 <= idx < len(app._fk_results):
            rid = app._fk_results[idx][1]
            entry = app._fk_entry
            entry.delete(0, "end")
            entry.insert(0, str(rid))
            self._fk_update_label(entry)
            dirty_cb = getattr(entry, "_dirty_cb", None)
            if dirty_cb is not None:
                dirty_cb()
        self._fk_hide()
        self._commit_after_focus_out()

    def _fk_open_first(self):
        app = self.app
        if app._fk_win is not None and app._fk_results:
            self._fk_select()
            return "break"
        # popup not shown yet (or no matches yet): show now if text is a name
        if app._fk_entry is None:
            return "break"
        entry = app._fk_entry
        target = getattr(entry, "_fk_target", None)
        if target is None:
            return "break"
        self._fk_show(entry, target)
        if app._fk_win is not None:
            self._fk_select()
        return "break"

    def _fk_move(self, event):
        app = self.app
        if app._fk_win is None or not app._fk_results:
            return
        lb = app._fk_lb
        sel = lb.curselection()
        idx = sel[0] if sel else -1
        n = len(app._fk_results)
        if event.keysym == "Down":
            idx = min(idx + 1, n - 1)
        else:
            idx = max(idx - 1, 0)
        lb.selection_clear(0, "end")
        lb.selection_set(idx)
        lb.see(idx)
        return "break"

    def _fk_hide(self):
        app = self.app
        if app._fk_win is not None:
            try:
                app._fk_win.destroy()
            except Exception:
                pass
        app._fk_win = None
        app._fk_lb = None
        app._fk_entry = None

    def _fk_check_focus(self):
        app = self.app
        try:
            f = app.root.focus_get()
        except Exception:
            f = None
        if app._fk_entry is not None and f is app._fk_entry:
            return
        if app._fk_lb is not None and f is app._fk_lb:
            return
        self._fk_hide()

    def _field_tip_text(self, key, table=None):
        """Brief (name + one-line desc) tooltip for a field, in the current UI
        language. table defaults to the detail window's own table; the mini
        editor passes the ACTUAL table of the row being edited so fields that
        only exist there (e.g. Turrets.IsDynamicTurret) resolve correctly."""
        info = field_info(table if table is not None else self.table,
                          key, self.app.tr.lang)
        if not info:
            return ""
        name, desc = info
        if desc and desc != name:
            return name + "\n" + desc
        return name

    def _build_form(self, inner, row, widgets, dirty_cb, table=None):
        """table = the row's OWN table; the mini editor must pass it, otherwise
        fields that only exist in the related table (IsDynamicTurret & co.)
        fall back to the raw English key."""
        tname = table if table is not None else self.table
        tr = self.app.tr
        app = self.app
        tk, ttk = self.app.tk, self.app.ttk
        c = app.c
        widgets.clear()
        inner.config(bg=c["bg"])
        inner.columnconfigure(1, weight=1)
        for i, (key, value) in enumerate(row.items()):
            label_text = app._field_label(tname or "", key)
            lab = tk.Label(inner, text=label_text, anchor="w", fg=c["label_key"], bg=c["bg"])
            lab._color_role = "label_key"
            lab.grid(row=i, column=0, sticky="nw", padx=(6, 8), pady=2)
            tip = self._field_tip_text(key, tname)
            if tip:
                # first line = original field name (原字条名, e.g. Cost)
                def _tip(key=key, tip=tip):
                    return key + "\n" + tip
                ToolTip(lab, _tip, colors=c)
            fk_target = FIELD_REF_MAP.get(key)
            is_fk = (fk_target is not None and fk_target in app.tables
                     and (isinstance(value, int) and not isinstance(value, bool)
                          or value is None))
            if is_fk:
                # smart FK field: type a name/HUD name to find the Id
                cell = tk.Frame(inner, bg=c["bg"])
                entry = tk.Entry(cell, width=18, bg=c["entry_bg"], fg=c["entry_fg"],
                                 insertbackground=c["entry_fg"])
                entry.pack(side="left")
                name_l = tk.Label(cell, anchor="w", fg=c["hint"], bg=c["bg"])
                name_l._color_role = "hint"
                name_l.pack(side="left", fill="x", expand=True, padx=6)
                entry.insert(0, str(value) if isinstance(value, int) else "")
                def handler(_e=None, entry=entry, name_l=name_l, target=fk_target):
                    dirty_cb()
                    self._fk_update_label(entry)
                    self._fk_schedule(entry, target)
                entry.bind("<KeyRelease>", handler)
                entry.bind("<Return>", lambda e: self._fk_open_first())
                entry.bind("<Down>", self._fk_move)
                entry.bind("<Up>", self._fk_move)
                entry.bind("<Escape>", lambda e: self._fk_hide())
                entry.bind("<FocusOut>", self._on_field_focus_out)
                cell.grid(row=i, column=1, sticky="ew", padx=4, pady=2)
                entry._fk_name_label = name_l
                entry._fk_target = fk_target
                entry._fk_field = key
                entry._dirty_cb = dirty_cb
                self._fk_update_label(entry)
                widgets[key] = (entry, value)
                continue
            ekey = enum_key(tname or "", key)
            if ekey and isinstance(value, int) and not isinstance(value, bool):
                # v1.8.55：枚举字段（单位大类 / 槽位类别 / 角色 / 武器类型 / 弹道…）
                # 右侧绿色箭头 ➜ 直接写出该 ID 对应的种类；点箭头或文字打开词典对照。
                cell = tk.Frame(inner, bg=c["bg"])
                entry = tk.Entry(cell, width=18, bg=c["entry_bg"], fg=c["entry_fg"],
                                 insertbackground=c["entry_fg"])
                entry.pack(side="left")
                entry.insert(0, str(value))
                name_l = tk.Label(cell, anchor="w", fg=c["ok"], bg=c["bg"], cursor="hand2")
                name_l._color_role = "ok"
                name_l.pack(side="left", fill="x", expand=True, padx=6)
                def ehandler(_e=None, entry=entry, name_l=name_l, ekey=ekey, tname=tname):
                    dirty_cb()
                    self._enum_update_label(entry, name_l, ekey, tname)
                entry.bind("<KeyRelease>", ehandler)
                entry.bind("<FocusOut>", self._on_field_focus_out)
                cell.grid(row=i, column=1, sticky="ew", padx=4, pady=2)
                entry._enum_key = ekey
                entry._enum_label = name_l
                entry._dirty_cb = dirty_cb
                self._enum_update_label(entry, name_l, ekey, tname)
                for w in (name_l,):
                    ToolTip(w, lambda ekey=ekey: self._enum_tip_text(ekey), colors=c)
                    w.bind("<Button-1>", lambda _e, ekey=ekey: app.show_dictionary(ekey))
                widgets[key] = (entry, value)
                continue
            if isinstance(value, bool):
                w = ttk.Combobox(inner, state="readonly", width=12,
                                 values=[tr.t("bool_true"), tr.t("bool_false")])
                w.set(tr.t("bool_true") if value else tr.t("bool_false"))
                w.bind("<<ComboboxSelected>>", dirty_cb)
                # disable the mouse wheel so scrolling the form cannot
                # accidentally flip the true/false value
                w.bind("<MouseWheel>", lambda e: "break")
            elif isinstance(value, int) and not isinstance(value, bool):
                w = tk.Entry(inner, width=46, bg=c["entry_bg"], fg=c["entry_fg"],
                             insertbackground=c["entry_fg"])
                w.insert(0, str(value))
                w.bind("<KeyRelease>", dirty_cb)
            elif isinstance(value, float):
                w = tk.Entry(inner, width=46, bg=c["entry_bg"], fg=c["entry_fg"],
                             insertbackground=c["entry_fg"])
                w.insert(0, repr(value))
                w.bind("<KeyRelease>", dirty_cb)
            elif value is None:
                w = tk.Entry(inner, width=46, bg=c["entry_bg"], fg=c["placeholder"],
                             insertbackground=c["entry_fg"])
                w._color_role = "placeholder"
                w.bind("<KeyRelease>", dirty_cb)
            elif isinstance(value, str) and len(value) > 200:
                w = tk.Text(inner, height=4, width=52, wrap="word",
                            bg=c["entry_bg"], fg=c["entry_fg"], insertbackground=c["entry_fg"])
                w.insert("1.0", value)
                w.bind("<<Modified>>", dirty_cb)
            else:
                w = tk.Entry(inner, width=46, bg=c["entry_bg"], fg=c["entry_fg"],
                             insertbackground=c["entry_fg"])
                w.insert(0, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                w.bind("<KeyRelease>", dirty_cb)
            w.bind("<FocusOut>", self._on_field_focus_out)
            w.grid(row=i, column=1, sticky="ew", padx=4, pady=2)
            widgets[key] = (w, value)

    def _view(self):
        """The entity currently shown (and editable) on the left pane."""
        if self.preview is not None:
            pt, pi = self.preview
            rows = self.app.tables.get(pt)
            if rows is not None and 0 <= pi < len(rows) and isinstance(rows[pi], dict):
                return (pt, pi)
            self.preview = None  # stale target, fall back to the owner
        return (self.table, self.row)

    def _render_left(self):
        """Render the left pane: an editable form of the viewed entity."""
        app = self.app
        self._clear_fields(self.fields_inner)
        self._widgets = {}
        self._pending = False
        vt, vi = self._view()
        rows = app.tables.get(vt)
        if rows is None or vi is None or not (0 <= vi < len(rows)) or not isinstance(rows[vi], dict):
            return
        row = rows[vi]
        if (vt, vi) != (self.table, self.row):
            name = row.get("Name") or row.get("HUDName") or row.get("ModelFileName") or ""
            rid = row.get("Id")
            header = f"🔍 {vt}"
            if name:
                header += f" — {name}"
            if isinstance(rid, int):
                header += f"  (Id={rid})"
            # header in a packed frame; the field form in a separate grid
            # frame (pack and grid cannot share one container)
            hf = app.tk.Frame(self.fields_inner, bg=app.c["bg"])
            hf.pack(fill="x", padx=6, pady=(2, 4))
            hl = app.tk.Label(hf, text=header, anchor="w",
                              font=(cjk_family(), 10, "bold"),
                              fg=app.c["accent"], bg=app.c["bg"])
            hl.pack(anchor="w")
            body = app.tk.Frame(self.fields_inner, bg=app.c["bg"])
            body.pack(fill="both", expand=True)
            self._build_form(body, row, self._widgets, self._dirty, table=vt)
        else:
            self._build_form(self.fields_inner, row, self._widgets, self._dirty, table=vt)
        app.theme_children(self.win)

    def rebuild(self):
        app = self.app
        tr = app.tr
        self._clear_fields(self.fields_inner)
        self._widgets = {}
        self._pending = False
        self.preview = None
        self._clear_mini()

        if self.table is not None and self.row_id is not None and self.table in app.tables:
            app._ensure_indexes()
            idx = app._id_index.get(self.table, {}).get(self.row_id)
            self.row = idx if idx is not None else None

        if self.table is None or self.table not in app.tables or self.row is None:
            self.rel_tree.delete(*self.rel_tree.get_children())
            self.rel_map = {}
            self.title_l.config(text=tr.t("editor_no_row"))
            self.clone_btn.pack_forget()
            self._refresh_info()
            return
        rows = app.tables[self.table]
        if not (0 <= self.row < len(rows)):
            self.rel_tree.delete(*self.rel_tree.get_children())
            self.rel_map = {}
            self.title_l.config(text=tr.t("editor_no_row"))
            self.clone_btn.pack_forget()
            self._refresh_info()
            return
        row = rows[self.row]
        if not isinstance(row, dict):
            self.rel_tree.delete(*self.rel_tree.get_children())
            self.rel_map = {}
            self.title_l.config(text=tr.t("editor_no_row"))
            self.clone_btn.pack_forget()
            self._refresh_info()
            return

        rid = row.get("Id")
        self.row_id = rid if isinstance(rid, int) else None
        self._update_title()
        self._refresh_info()
        if self.table == "Units":
            self.clone_btn.pack(side="right", padx=3, before=self.dup_btn)
        else:
            self.clone_btn.pack_forget()

        self._render_left()
        app._build_relations_into(self.rel_tree, self.rel_map,
                                  self.table, self.row, self)
        app.theme_children(self.win)

    def _row_icon_name(self, table, row):
        """Which game-icon asset a row uses for the big detail banner."""
        if not isinstance(row, dict):
            return None
        if table == "Units":
            return row.get("ThumbnailFileName")
        if table in ("Weapons", "Ammunitions"):
            return row.get("HUDIcon")
        if table == "Options":
            return row.get("OptionPicture")
        if table == "Specializations":
            return row.get("Icon")
        return None

    def _update_title(self):
        """Refresh just the window title label for the viewed row, plus the
        big banner box (icon + localized name) for units/weapons/ammo etc."""
        app = self.app
        vt, vi = self._view()
        if vt is None or vt not in app.tables or vi is None:
            return
        rows = app.tables[vt]
        if not (0 <= vi < len(rows)):
            return
        row = rows[vi]
        if not isinstance(row, dict):
            return
        rid = row.get("Id")
        name = row.get("Name") or row.get("HUDName") or row.get("ModelFileName") or ""
        title = f"{vt}  —  {name}" if name else vt
        if isinstance(rid, int):
            title += f"  (Id={rid})"
        self.title_l.config(text=title)
        # banner: big icon box + localized name
        icon_name = self._row_icon_name(vt, row)
        img = game_icon_image(app.root, icon_name, 200, 52) if icon_name else None
        self._banner_icon_ref = img  # keep the PhotoImage alive
        if img is not None:
            self.banner_icon_l.config(image=img, text="")
        else:
            self.banner_icon_l.config(image="", text="")
        disp = app._display_name(vt, row)
        self.banner_name_l.config(text=disp if disp else name)

    def _rel_needs_rebuild(self, old_row, new_row):
        """True when a relation-tree/title-affecting field changed between the
        two row snapshots (Id, FK ids, or display fields)."""
        for key in set(old_row) | set(new_row):
            if old_row.get(key) != new_row.get(key):
                if key in FIELD_REF_MAP or key in REL_DISPLAY_FIELDS:
                    return True
        return False

    def _clear_mini(self):
        self._clear_fields(self.mini_inner)
        self.mini_widgets = {}
        self.mini_state = None
        self.mini_pending = False
        self.mini_label_l.config(text=self.app.tr.t("rel_mini_hint"))
        self.mini_label_l._color_role = "hint"

    def _read_form(self, widgets, row):
        app = self.app
        new_row = dict(row)
        bad = []
        resolved_fk = {}
        for key, orig in row.items():
            entry = widgets.get(key)
            if entry is None:
                continue
            widget, _ = entry
            tk, ttk = app.tk, app.ttk
            if isinstance(widget, ttk.Combobox):
                val = widget.get() == app.tr.t("bool_true")
                ok = True
            else:
                if isinstance(widget, tk.Text):
                    text = widget.get("1.0", "end-1c")
                else:
                    text = widget.get()
                fk_target = FIELD_REF_MAP.get(key)
                fk_typed_name = (fk_target is not None and fk_target in app.tables
                                 and isinstance(orig, (int, type(None)))
                                 and not isinstance(orig, bool)
                                 and bool(text.strip())
                                 and not text.strip().lstrip("-").isdigit())
                if fk_typed_name:
                    # the user typed a name / HUD name instead of an Id
                    rid, cnt = app.resolve_name_in_table(fk_target, text)
                    if cnt == 1:
                        val, ok = rid, True
                        resolved_fk[key] = rid
                    else:
                        val, ok = None, False
                elif isinstance(orig, bool):
                    val = text.strip().lower() in ("true", "1", "yes")
                    ok = True
                elif isinstance(orig, int) and not isinstance(orig, bool):
                    try:
                        val = int(text.strip())
                        ok = True
                    except ValueError:
                        val, ok = None, False
                elif isinstance(orig, float):
                    try:
                        val = float(text.strip())
                        ok = True
                    except ValueError:
                        val, ok = None, False
                elif orig is None:
                    if text.strip() == "":
                        val, ok = None, True
                    else:
                        try:
                            val = json.loads(text.strip())
                            ok = True
                        except Exception:
                            val, ok = None, False
                elif isinstance(orig, str):
                    val, ok = text, True
                else:
                    try:
                        val = json.loads(text.strip())
                        ok = True
                    except Exception:
                        val, ok = text, True
            if ok:
                new_row[key] = val
            else:
                bad.append(key)
        return new_row, bad, resolved_fk

    def _apply_form(self, table, idx, new_row, old_row):
        app = self.app
        rows = app.tables[table]
        if new_row == old_row:
            return True
        old_full = list(rows)
        rows[idx] = new_row
        app.undo.push(table, old_full, list(rows), "edit")
        app._mark_dirty()
        if app._fmt_ver.get(table) == app._data_version - 1:
            app._ensure_indexes()
            app._fmt_ver[table] = app._data_version
            fmts = app._fmt_cache[table]
            cols = app._table_columns(table)
            fmts[idx] = app._fmt_row(new_row, cols, table)
        app.refresh_visible_row(idx)
        # keep the unit's SquadWeapons pool in sync when a squad member's
        # primary/secondary weapon changes, and auto-attach the most common ammo
        if table == "SquadMembers":
            unit_id = new_row.get("UnitId")
            for f in ("PrimaryWeaponId", "SpecialWeaponId"):
                v = new_row.get(f)
                if v != old_row.get(f):
                    app._sync_squad_weapon(unit_id, v)
                    if isinstance(v, int) and v > 0:
                        app._auto_add_weapon_ammo(unit_id, v)
        return True

    def commit(self, silent=False):
        app = self.app
        if not self._pending:
            return True
        vt, vi = self._view()
        if vt is None or vi is None:
            return True
        rows = app.tables.get(vt)
        if rows is None or not (0 <= vi < len(rows)):
            self._pending = False
            return True
        old_row = rows[vi]
        if not isinstance(old_row, dict):
            self._pending = False
            return True
        new_row, bad, resolved_fk = self._read_form(self._widgets, old_row)
        if bad:
            if not silent:
                app._show_error(": ".join((app.tr.t("error_title"), ", ".join(bad))))
            return False
        self._apply_form(vt, vi, new_row, old_row)
        self._pending = False
        if vt == self.table and vi == self.row and isinstance(new_row.get("Id"), int):
            self.row_id = new_row["Id"]
        # incremental refresh: fix only the FK fields that were name-resolved,
        # in place (no full form rebuild)
        for key, rid in resolved_fk.items():
            entry = self._widgets.get(key)
            if entry is not None:
                widget, _ = entry
                widget.delete(0, "end")
                widget.insert(0, str(rid))
                self._fk_update_label(widget)
        # rebuild the relation tree when a relation/display field changed
        # (either the owner's own row, or a related entity shown on the left
        # whose name/FK is displayed in the tree)
        if self._rel_needs_rebuild(old_row, new_row):
            app._build_relations_into(self.rel_tree, self.rel_map,
                                      self.table, self.row, self)
        self._update_title()
        return True

    def commit_mini(self, silent=False):
        app = self.app
        if not self.mini_pending or self.mini_state is None:
            return True
        table, idx = self.mini_state
        rows = app.tables.get(table)
        if rows is None or not (0 <= idx < len(rows)):
            self._clear_mini()
            return True
        old_row = rows[idx]
        if not isinstance(old_row, dict):
            self._clear_mini()
            return True
        new_row, bad, resolved_fk = self._read_form(self.mini_widgets, old_row)
        if bad:
            if not silent:
                app._show_error(": ".join((app.tr.t("error_title"), ", ".join(bad))))
            return False
        self._apply_form(table, idx, new_row, old_row)
        self.mini_pending = False
        # fix only the FK fields that were name-resolved, in place
        for key, rid in resolved_fk.items():
            entry = self.mini_widgets.get(key)
            if entry is not None:
                widget, _ = entry
                widget.delete(0, "end")
                widget.insert(0, str(rid))
                self._fk_update_label(widget)
        # update the mini label in case Id/name changed
        if self.mini_state == (table, idx) and isinstance(rows[idx], dict):
            row = rows[idx]
            name = row.get("Name") or row.get("HUDName") or ""
            self.mini_label_l.config(text=f"{table} — {name} (Id={row.get('Id')})")
            self.mini_label_l._color_role = "label_key"
        # the relation tree shows link-row fields, so refresh it (affected row only)
        app._build_relations_into(self.rel_tree, self.rel_map,
                                  self.table, self.row, self)
        return True

    def _load_mini(self, table, idx):
        app = self.app
        tr = app.tr
        self._clear_fields(self.mini_inner)
        self.mini_widgets = {}
        self.mini_pending = False
        if table is None or table not in app.tables or not (0 <= idx < len(app.tables[table])):
            self.mini_state = None
            self.mini_label_l.config(text=tr.t("rel_mini_label"))
            return
        row = app.tables[table][idx]
        if not isinstance(row, dict):
            self.mini_state = None
            self.mini_label_l.config(text=tr.t("rel_mini_label"))
            return
        self.mini_state = (table, idx)
        name = row.get("Name") or row.get("HUDName") or ""
        rid = row.get("Id")
        self.mini_label_l.config(text=f"{table} — {name} (Id={rid})")
        self.mini_label_l._color_role = "label_key"
        self._build_form(self.mini_inner, row, self.mini_widgets, self._mini_dirty,
                         table=table)
        app.theme_children(self.win)

    def on_rel_select(self, _event=None):
        sel = self.rel_tree.selection()
        if not sel:
            return
        iid = sel[0]
        payload = self.rel_map.get(iid)
        if not payload:
            return
        nav = payload.get("nav")
        edit = payload.get("edit")
        kind = payload.get("kind")
        suppress = getattr(self, "_nav_suppress", False)

        # commit the editable form before swapping the left entity
        if self._pending:
            if not self.commit(silent=False):
                return

        # decide the new left entity; the relations tree stays on the owner
        if suppress:
            new_preview = self.preview
        elif kind == "unit":
            new_preview = None
        elif nav and nav[1] is not None:
            new_preview = None if (nav[0], nav[1]) == (self.table, self.row) \
                else (nav[0], nav[1])
        else:
            new_preview = self.preview  # groups keep the current left entity

        if new_preview != self.preview:
            self.preview = new_preview
            self._render_left()
            self._update_title()
            self._refresh_info()

        # bottom mini editor shows the link row (relationship data)
        if edit and edit[1] is not None:
            if self.mini_pending and self.mini_state and self.mini_state != (edit[0], edit[1]):
                self.commit_mini(silent=False)
            self._load_mini(edit[0], edit[1])
        else:
            self._clear_mini()

    def _on_rel_click(self, event):
        """Left-click toggles a fold when it lands on the leading "▼ / ▶"
        marker or on the row's icon image; the rest of the row just selects.
        The built-in ttk triangle is hidden and the marker is drawn at the
        start of the row text, so we measure the marker's pixel span from the
        text's left edge."""
        row = self.rel_tree.identify("row", event.x, event.y)
        if not row or not self.rel_tree.get_children(row):
            return
        text = self.rel_tree.item(row, "text")
        if text.startswith(FOLD_OPEN):
            mark = FOLD_OPEN
        elif text.startswith(FOLD_CLOSED):
            mark = FOLD_CLOSED
        else:
            return
        # Clicking the coloured icon (group rows) toggles just like the marker.
        hit = self.rel_tree.identify("element", event.x, event.y) == "image"
        if not hit:
            bbox = self.rel_tree.bbox(row, "#0")
            if not bbox:
                return
            # The text begins after the cell padding (and any icon image); scan
            # right from the cell's left edge until the text element starts.
            # Scan along the row's vertical centre so tall rows still hit it.
            y_mid = bbox[1] + bbox[3] // 2
            text_x = None
            for dx in range(0, 80):
                if self.rel_tree.identify("element", bbox[0] + dx, y_mid) == "text":
                    text_x = bbox[0] + dx
                    break
            if text_x is None:
                return
            try:
                import tkinter.font as tkfont
                mark_w = tkfont.Font(self.app.root, font=self.app._rel_font).measure(mark)
            except Exception:
                mark_w = 20
            hit = text_x <= event.x <= text_x + mark_w + 6
        if hit:
            new_open = not self.rel_tree.item(row, "open")
            self.rel_tree.item(row, open=new_open)
            self.app._update_fold_mark(self.rel_tree, self.rel_map, row)
            key = self.app._rel_state_key(self.rel_map.get(row), self.rel_tree.item(row, "text"))
            self.app._rel_fold[key] = new_open
            return "break"

    def on_rel_context(self, event):
        """Right-click: select the item under the cursor and show its menu."""
        iid = self.rel_tree.identify("row", event.x, event.y)
        if iid:
            # selecting on right-click must not drill down; only left-clicks
            # navigate. Keep the flag set through the <<TreeviewSelect>> event.
            self._nav_suppress = True
            self.rel_tree.selection_set(iid)
            self.app.root.after_idle(lambda: setattr(self, "_nav_suppress", False))
        menu = self._build_context_menu(iid)
        if menu is not None:
            self.app._post_menu(menu)
        return "break"

    def _menu_generic(self, menu, payload):
        """Common actions for a concrete relation item (open / duplicate / delete)."""
        tr = self.app.tr
        nav = payload.get("nav")
        dup = payload.get("dup", payload.get("edit"))
        delete = payload.get("delete", payload.get("edit"))
        if nav and nav[1] is not None:
            menu.add_command(label=tr.t("rel_menu_open"),
                             command=lambda: self.load(nav[0], nav[1], push=True))
        if dup and dup[1] is not None:
            menu.add_command(label=tr.t("rel_duplicate"),
                             command=self.on_rel_duplicate)
        if delete and delete[1] is not None:
            menu.add_command(label=tr.t("btn_del"),
                             command=self.on_rel_delete)

    def _unit_add_menu(self):
        """Direct 'add empty row' items for a unit (no long pick-lists)."""
        tr = self.app.tr
        app = self.app
        menu = app.tk.Menu(self.win, tearoff=0)
        menu.add_command(label=tr.t("rel_qadd_ability"),
                         command=lambda: app.add_unit_link(self, "UnitAbilities", "AbilityId", 0))
        menu.add_command(label=tr.t("rel_qadd_turret"),
                         command=lambda: app.add_new_turret(self))
        menu.add_command(label=tr.t("rel_qadd_turret_link"),
                         command=lambda: app.add_mount_turret(self))
        menu.add_command(label=tr.t("rel_qadd_squad_weapon"),
                         command=lambda: app.add_unit_link(self, "SquadWeapons", "WeaponId", 0, {"Order": 0}))
        menu.add_command(label=tr.t("rel_qadd_squad_member"),
                         command=lambda: app.add_squad_member(self))
        menu.add_command(label=tr.t("rel_qadd_armor"),
                         command=lambda: app.add_unit_link(self, "UnitArmors", "ArmorId", 0))
        menu.add_command(label=tr.t("rel_qadd_mobility"),
                         command=lambda: app.add_unit_link(self, "UnitPropulsions", "MobilityId", 0))
        menu.add_command(label=tr.t("rel_qadd_sensor"),
                         command=lambda: app.add_unit_link(self, "SensorUnits", "SensorId", 0))
        menu.add_command(label=tr.t("rel_qadd_mod"),
                         command=lambda: app.add_mod_slot(self))
        return menu

    def _group_menu(self, group):
        """Right-click a group (fold bar): add an empty row, or delete the whole
        group."""
        tr = self.app.tr
        app = self.app
        menu = app.tk.Menu(self.win, tearoff=0)
        has_add = True
        if group == "abilities":
            menu.add_command(label=tr.t("rel_qadd_ability"),
                             command=lambda: app.add_unit_link(self, "UnitAbilities", "AbilityId", 0))
        elif group == "turrets":
            menu.add_command(label=tr.t("rel_qadd_turret"), command=lambda: app.add_new_turret(self))
        elif group == "squad_weapons":
            menu.add_command(label=tr.t("rel_qadd_squad_weapon"),
                             command=lambda: app.add_unit_link(self, "SquadWeapons", "WeaponId", 0, {"Order": 0}))
        elif group == "squad_members":
            menu.add_command(label=tr.t("rel_qadd_squad_member"), command=lambda: app.add_squad_member(self))
        elif group == "armors":
            menu.add_command(label=tr.t("rel_qadd_armor"),
                             command=lambda: app.add_unit_link(self, "UnitArmors", "ArmorId", 0))
        elif group == "mobility":
            menu.add_command(label=tr.t("rel_qadd_mobility"),
                             command=lambda: app.add_unit_link(self, "UnitPropulsions", "MobilityId", 0))
            menu.add_command(label=tr.t("rel_qadd_flypreset"),
                             command=lambda: app.add_flypreset(self))
        elif group == "sensors":
            menu.add_command(label=tr.t("rel_qadd_sensor"),
                             command=lambda: app.add_unit_link(self, "SensorUnits", "SensorId", 0))
        elif group == "mods":
            menu.add_command(label=tr.t("rel_qadd_mod"), command=lambda: app.add_mod_slot(self))
        elif group == "ammo":
            has_add = False  # ammo is added via weapons; only delete is offered
        else:
            menu.destroy()
            return None
        if has_add:
            menu.add_separator()
        menu.add_command(label=tr.t("rel_delete_group"), command=self.on_rel_delete)
        return menu

    def _build_context_menu(self, iid):
        """Build the right-click menu for a relation-tree node (or None)."""
        tr = self.app.tr
        app = self.app
        if not iid:
            return None
        payload = self.rel_map.get(iid)
        if not payload:
            return None
        kind = payload.get("kind")

        if kind == "unit":
            return self._unit_add_menu()
        if kind == "group":
            return self._group_menu(payload.get("group"))

        # concrete item
        edit = payload.get("edit")
        nav = payload.get("nav")
        edit_table = edit[0] if (edit and edit[1] is not None) else None
        nav_table = nav[0] if (nav and nav[1] is not None) else None

        menu = app.tk.Menu(self.win, tearoff=0)

        if edit_table == "Modifications":
            mod_id = app.tables["Modifications"][edit[1]].get("Id")
            if isinstance(mod_id, int):
                menu.add_command(label=tr.t("rel_qadd_option"),
                                 command=lambda: app.add_option_to_mod(self, mod_id))
                menu.add_separator()
        elif nav_table == "Turrets":
            # add an empty weapon to this turret; user fills WeaponId
            turret_id = app.tables["Turrets"][nav[1]].get("Id")
            if isinstance(turret_id, int):
                menu.add_command(label=tr.t("rel_qadd_weapon"),
                                 command=lambda: app.add_turret_weapon(self, turret_id))
                menu.add_separator()
        elif nav_table == "Weapons":
            # add an empty ammo binding for this weapon; user fills AmmunitionId
            uid = app._current_unit_id(self)
            wid = app.tables["Weapons"][nav[1]].get("Id")
            if uid is not None and isinstance(wid, int):
                menu.add_command(label=tr.t("rel_qadd_ammo"),
                                 command=lambda: app.add_weapon_ammo(self, uid, wid, 0))
                menu.add_separator()

        self._menu_generic(menu, payload)
        return menu

    def on_rel_double(self, _event=None):
        sel = self.rel_tree.selection()
        if not sel:
            return
        payload = self.rel_map.get(sel[0])
        if not payload:
            return
        target = payload.get("nav")
        if not target or target[1] is None:
            return
        self.load(target[0], target[1], push=True)

    def on_rel_delete(self, _event=None):
        sel = self.rel_tree.selection()
        if not sel:
            return
        payload = self.rel_map.get(sel[0])
        if not payload:
            return
        if payload.get("kind") == "group":
            if self.app.delete_unit_group(self, payload.get("group")):
                self._clear_mini()
                self.rebuild()
            return
        target = payload.get("delete", payload.get("edit"))
        if not target or target[1] is None:
            return
        table, idx = target
        if table == self.table and idx == self.row:
            return
        if not self.app.delete_table_row(table, idx):
            return
        self._clear_mini()
        self.rebuild()

    def on_rel_duplicate(self, _event=None):
        """Copy the selected relation item into a new row (new Id) in the same
        table — e.g. duplicate an infantry weapon, a squad member, or an ammo
        binding directly from the relation tree."""
        sel = self.rel_tree.selection()
        if not sel:
            return
        payload = self.rel_map.get(sel[0])
        if not payload:
            return
        target = payload.get("dup", payload.get("edit"))
        if not target or target[1] is None:
            return
        table, idx = target
        if table not in self.app.tables:
            return
        if not self.commit(silent=False) or not self.commit_mini(silent=False):
            return
        new_idx = self.app.duplicate_table_row(table, idx)
        if new_idx is None:
            return
        self._clear_mini()
        self.app._build_relations_into(self.rel_tree, self.rel_map,
                                       self.table, self.row, self)
        self._load_mini(table, new_idx)
        self.app._toast(self.app.tr.t("new_id_allocated",
                                      id=self.app.tables[table][new_idx].get("Id")))
        return "break"

    def duplicate_current(self):
        vt, vi = self._view()
        if vt is None or vi is None:
            return
        if not self.commit(silent=False) or not self.commit_mini(silent=False):
            return
        new_idx = self.app.duplicate_table_row(vt, vi)
        if new_idx is None:
            return
        self.load(vt, new_idx, push=True)
        self.app._toast(self.app.tr.t("new_id_allocated",
                                      id=self.app.tables[vt][new_idx].get("Id")))

    def clone_current(self):
        vt, vi = self._view()
        if vt != "Units" or vi is None:
            return
        if not self.commit(silent=False) or not self.commit_mini(silent=False):
            return
        new_idx = self.app.clone_unit(vi)
        if new_idx is None:
            return
        self.load("Units", new_idx, push=True)
        self.app._toast(self.app.tr.t("new_id_allocated",
                                      id=self.app.tables["Units"][new_idx].get("Id")))

    def refresh(self):
        if self.table is None:
            return
        self.commit(silent=True)
        self.commit_mini(silent=True)
        self.rebuild()

    def close(self):
        if self._auto_after is not None:
            self.app.root.after_cancel(self._auto_after)
            self._auto_after = None
        if self.table is not None and (not self.commit(silent=False) or not self.commit_mini(silent=False)):
            return
        self.lookup_box.hide()
        if self in self.app.details:
            self.app.details.remove(self)
        self.win.destroy()


class EditorApp:
    """Main application window.

    Holds the table list, the search/filter toolbar, the name→Id lookup box and
    the undo stack. Owns self.tables (decrypted data) plus the lazily-built
    indexes (_id_index / _by_unit / _by_turret / _by_mod). Opens DetailWindow
    instances for drilling into a row.
    """

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.tr = Translator()
        settings = load_settings()
        lang = settings.get("language", "zh")
        self.tr.set(lang if lang in LANGS else "zh")
        self.theme = "dark"  # dark-only UI (light mode removed)
        self._icon_refs = {}
        self._menus = []
        self._rowheight = 24
        self._rel_rowheight = 36
        self._rel_font = ("", 10)
        self._blank_indicator_img = None

        # dark color palette (black background + light blue)
        self.c = {
            "bg": "#000000", "panel": "#000000", "fg": "#b8cdff",
            "label_key": "#7fb2ff", "hint": "#7d8aa5", "ok": "#6ee7a0",
            "warn": "#ff8a80", "placeholder": "#5c6b84",
            "entry_bg": "#0c111c", "entry_fg": "#cfe0ff",
            "btn_bg": "#0c1016", "btn_fg": "#b8cdff", "btn_active": "#1c2b3f",
            "sel": "#1f5fbf", "sel_fg": "#ffffff",
            "list_bg": "#000000", "list_fg": "#cfe0ff",
            "tree_bg": "#000000", "tree_fg": "#cfe0ff",
            "menu_bg": "#000000", "menu_fg": "#b8cdff",
            "border": "#2a3b52", "accent": "#5b9dff", "disabled": "#5c6b84",
        }

        self.tables = {}
        self.dump_obj = None
        self.dump_path = None
        self.folder_path = None
        self.source_is_dump = False
        self.unity3d_path = None
        self.dirty = False
        self.undo = UndoStack()
        self.current_table = None
        self.current_row = None
        self.filtered_map = []
        self.filter_text = ""
        self.filter_column = None
        self.details = []

        # caches (invalidated via self._data_version)
        self._data_version = 0
        self._cols_cache = {}
        self._cols_ver = -1
        self._fmt_cache = {}
        self._fmt_ver = {}
        self._fmt_lang = None   # formatted cells are localized -> keyed by language too
        self._rel_ver = -1
        self._id_index = {}
        self._by_unit = {}
        self._by_turret = {}
        self._by_mod = {}
        self._rel_fold = {}
        self._search_after = None
        self._fill_gen = 0
        self._toast_after = None
        self._unit_cards = {}
        self._unit_icons = {}
        self._unit_selected = None
        self._unit_selected_idx = None
        self._unit_redraw_after = None
        self._unit_folds = {}
        self._unit_active_fold = None
        self._wheel_target = None
        self._fk_after = None
        self._fk_win = None
        self._fk_lb = None
        self._fk_entry = None
        self._fk_label = None
        self._fk_results = []

        root.title(app_title_text(self.tr))
        root.geometry("1500x880")
        root.minsize(1100, 640)
        try:
            ttk.Style().theme_use("clam")
        except Exception:
            pass

        self._setup_cjk_font()
        self._build_ui()
        self._bind_keys()
        self._apply_lang()
        self.apply_theme()
        self.root.bind_all("<MouseWheel>", self._wheel_global, add="+")

    def _setup_cjk_font(self):
        """Use 新宋体 (NSimSun) for the UI; GB2312 also covers Cyrillic (ru)."""
        global _CJK_FAMILY
        try:
            import tkinter.font as tkfont
            fams = set(tkfont.families(self.root))
            fam = pick_cjk_font(fams)
            if not fam:
                return
            self._cjk_font_family = fam
            _CJK_FAMILY = fam
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkTooltipFont",
                         "TkIconFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(name).configure(family=fam, size=12)
                except Exception:
                    pass
            for name in ("TkHeadingFont", "TkCaptionFont"):
                try:
                    tkfont.nametofont(name).configure(family=fam, size=14)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------------- UI ----------------

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        root = self.root

        # Give tree rows enough height so descenders (g/y/p…) are not clipped
        # AND the color emoji icons in relation trees fit fully (no bottom cut).
        try:
            import tkinter.font as tkfont
            lh = tkfont.nametofont("TkDefaultFont").metrics("linespace")
            ref = icon_image(self.root, "1f4c2", 16)
            icon_h = ref.height() if ref is not None else 16
            self._rowheight = max(int(lh) + 6, icon_h + 8)
        except Exception:
            self._rowheight = 24
        # The relation tree gets bigger rows + a slightly larger font so the
        # custom "▼ / ▶" fold indicator is easy to see and click.
        fam = getattr(self, "_cjk_font_family", None) or ""
        self._rel_font = (fam, 12)
        self._rel_rowheight = max(self._rowheight + 26, 68)
        self._unit_font = (fam, 13)
        self._unit_rowheight = max(self._rowheight + 16, 46)
        # Data table grid: keep NSimSun (新宋体), slightly larger.
        fam = getattr(self, "_cjk_font_family", None) or ""
        self._data_font = (fam, 12)

        # custom-drawn menu bar (the native Windows menu strip cannot be
        # recolored, so we draw our own so it matches the dark theme)
        self.menubar_frame = tk.Frame(root,
                                      highlightbackground=self.c["border"], highlightthickness=1)
        self.menubar_frame.pack(side="top", fill="x")

        self.toolbar = tk.Frame(root, padx=4, pady=3,
                                 highlightbackground=self.c["border"], highlightthickness=1)
        self.toolbar.pack(side="top", fill="x")
        self.tool_buttons = {}
        for key, cmd in (("open", self.open_dump_dialog), ("unity3d", self.open_from_unity3d),
                         ("import_unity3d", self.import_into_unity3d), ("save", self.save),
                         ("undo", self.do_undo), ("redo", self.do_redo),
                         ("add", self.add_row), ("dup", self.duplicate_row),
                         ("del", self.delete_row),
                         ("refs", self.find_references_dialog),
                         ("validate", self.validate_dialog)):
            b = tk.Button(self.toolbar, command=cmd, padx=6)
            self.tool_buttons[key] = b

        # Two-row grid: every button gets an equal-width cell (stretches to
        # fill the row), so the toolbar stays tidy and nothing is clipped in
        # narrow (non-maximized) windows.
        for c in range(6):
            self.toolbar.columnconfigure(c, weight=1, uniform="tb0")
        for c, key in enumerate(("open", "save", "unity3d", "import_unity3d", "undo", "redo")):
            self.tool_buttons[key].grid(row=0, column=c, sticky="ew", padx=2, pady=2)
        self.lang_btn = tk.Button(self.toolbar, command=self.cycle_language, padx=6)
        self.lang_btn.grid(row=0, column=6, sticky="e", padx=(10, 2), pady=2)
        self.dict_btn = tk.Button(self.toolbar, command=self.show_dictionary, padx=6)
        self.dict_btn.grid(row=0, column=7, sticky="e", padx=(2, 6), pady=2)

        for c in range(5):
            self.toolbar.columnconfigure(c, weight=1, uniform="tb1")
        self.toolbar.columnconfigure(6, weight=2)  # lookup entry: ~2 button widths
        for c, key in enumerate(("add", "dup", "del", "refs", "validate")):
            self.tool_buttons[key].grid(row=1, column=c, sticky="ew", padx=2, pady=2)
        # inline global lookup (name / HUD / UIName / option picture / thumbnail / HUD icon / Id)
        self.lookup_label_w = tk.Label(self.toolbar)
        self.lookup_label_w.grid(row=1, column=5, sticky="e", padx=(8, 2), pady=2)
        self.lookup_box = LookupBox(self, self.toolbar)
        self.lookup_box.entry.grid(row=1, column=6, columnspan=3, sticky="ew", padx=2, pady=2)

        self.paned = tk.PanedWindow(root, orient="horizontal", sashwidth=5)
        self.paned.pack(fill="both", expand=True)

        left = tk.Frame(self.paned)
        self.table_label_w = tk.Label(left, anchor="w")
        self.table_label_w.pack(fill="x", padx=6, pady=(4, 0))
        self.table_list = tk.Listbox(left, exportselection=False, width=30)
        self.table_list.pack(fill="both", expand=True, padx=6, pady=4)
        self.table_list.bind("<<ListboxSelect>>", self.on_table_select)
        self.bind_wheel(self.table_list)
        self.paned.add(left, minsize=200, stretch="never")

        right = tk.Frame(self.paned)
        self.paned.add(right, stretch="always")

        # Main-area notebook: the unit browser takes the whole panel for a
        # big, intuitive hierarchy; the raw table grid lives on a second tab.
        self.main_notebook = ttk.Notebook(right)
        self.main_notebook.pack(fill="both", expand=True)

        # Tab 1: unit browser — card / panel style so icons read clearly.
        unit_tab = tk.Frame(self.main_notebook)
        unit_wrap = tk.Frame(unit_tab)
        unit_wrap.pack(fill="both", expand=True)
        self.unit_canvas = tk.Canvas(unit_wrap, highlightthickness=0, bd=0,
                                     bg=self.c["bg"])
        self.unit_vsb = ttk.Scrollbar(unit_wrap, orient="vertical",
                                      command=self.unit_canvas.yview)
        self.unit_canvas.configure(yscrollcommand=self.unit_vsb.set)
        self.unit_vsb.pack(side="right", fill="y")
        self.unit_canvas.pack(side="left", fill="both", expand=True)
        self.unit_canvas.bind("<Button-1>", self.on_unit_click)
        self.unit_canvas.bind("<Double-1>", self.on_unit_double_click)
        self.unit_canvas.bind("<Configure>", self._on_unit_canvas_resize)
        self.bind_wheel(self.unit_canvas)
        self.main_notebook.add(unit_tab, text=self.tr.t("tab_units"))

        # Tab 2: raw table grid (search + columns)
        grid_tab = tk.Frame(self.main_notebook)
        search_frame = tk.Frame(grid_tab)
        self.search_label_w = tk.Label(search_frame)
        self.search_label_w.pack(side="left", padx=(4, 2))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var, width=42)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=2)
        self.search_entry.bind("<KeyRelease>", self.schedule_filter)
        self.col_combo = ttk.Combobox(search_frame, state="readonly", width=20)
        self.col_combo.pack(side="left", padx=2)
        self.col_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        self.clear_btn = tk.Button(search_frame, text="✕", command=self.clear_filter, padx=6)
        self.clear_btn.pack(side="left", padx=2)
        search_frame.pack(fill="x", padx=4, pady=(4, 0))

        tree_frame = tk.Frame(grid_tab)
        self.tree = ttk.Treeview(tree_frame, show="headings", selectmode="browse")
        self.vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)
        self.tree.bind("<Double-1>", self.on_row_double_click)
        self.tree.bind("<Delete>", lambda e: self.delete_row())
        self.tree.bind("<Return>", lambda e: self.open_detail(self.current_table, self.current_row))
        self.bind_wheel(self.tree)
        tree_frame.pack(fill="both", expand=True, padx=4)

        self.main_notebook.add(grid_tab, text=self.tr.t("tab_tables"))

        # 模型 Mod 板块已移除（迁移到 Blender 插件 v2.0）

        self.status = tk.Frame(root, highlightbackground=self.c["border"], highlightthickness=1)
        self.status_file_l = tk.Label(self.status, anchor="w")
        self.status_file_l.pack(side="left", padx=6)
        self.status_dirty_l = tk.Label(self.status, anchor="w")
        self.status_dirty_l.pack(side="left", padx=12)
        self.status_rows_l = tk.Label(self.status, anchor="w")
        self.status_rows_l.pack(side="left", padx=12)
        self.status_lang_l = tk.Label(self.status, anchor="e")
        self.status_lang_l.pack(side="right", padx=6)
        self.toast_l = tk.Label(self.status, anchor="e")
        self.toast_l._color_role = "ok"
        self.toast_l.pack(side="right", padx=10)
        self.status.pack(side="bottom", fill="x")

    def _bind_keys(self):
        r = self.root
        r.bind("<Control-o>", lambda e: self.open_dump_dialog())
        r.bind("<Control-O>", lambda e: self.open_dump_dialog())
        r.bind("<Control-Shift-o>", lambda e: self.open_folder_dialog())
        r.bind("<Control-Shift-O>", lambda e: self.open_folder_dialog())
        r.bind("<Control-s>", lambda e: self.save())
        r.bind("<Control-S>", lambda e: self.save())
        r.bind("<Control-z>", lambda e: self.do_undo())
        r.bind("<Control-y>", lambda e: self.do_redo())
        r.bind("<Control-Y>", lambda e: self.do_redo())
        r.bind("<F3>", lambda e: self._focus_lookup())
        r.bind("<Control-f>", lambda e: self._focus_lookup())
        r.bind("<Control-F>", lambda e: self._focus_lookup())
        r.bind("<F7>", lambda e: self.validate_dialog())
        r.bind("<F10>", lambda e: self.cycle_language())
        r.bind("<F1>", lambda e: self.show_help())

    # ---------------- detail window ----------------

    def open_detail(self, table, idx):
        if table is None or idx is None or table not in self.tables:
            return
        win = DetailWindow(self)
        self.details.append(win)
        win.load(table, idx, push=True)
        win.win.lift()
        win.win.focus_force()

    def close_detail(self):
        wins = list(self.details)
        self.details = []
        for d in wins:
            try:
                d.win.destroy()
            except Exception:
                pass

    def _refresh_detail(self):
        for d in list(self.details):
            d.refresh()

    def commit_editor(self, silent=False):
        """Wrapper: commit pending edits in all open detail windows."""
        ok = True
        for d in list(self.details):
            if not d.commit(silent=silent) or not d.commit_mini(silent=silent):
                ok = False
        return ok

    # ---------------- mouse wheel ----------------

    def bind_wheel(self, widget):
        """Wheel over this widget scrolls it, without clicking first."""
        widget.bind("<Enter>", lambda e: self._set_wheel_target(widget), add="+")
        widget.bind("<Leave>", lambda e: self._clear_wheel_target(widget), add="+")

    def _set_wheel_target(self, widget):
        self._wheel_target = widget

    def _clear_wheel_target(self, widget):
        if getattr(self, "_wheel_target", None) is widget:
            self._wheel_target = None

    def _wheel_global(self, event):
        w = getattr(self, "_wheel_target", None)
        if w is None or not w.winfo_exists():
            return
        try:
            step = -1 if event.delta > 0 else 1
            w.yview_scroll(step, "units")
        except Exception:
            pass
        return "break"

    # ---------------- theme ----------------

    def _set_title_bar(self, win, dark):
        """Dark/light OS title bar (Windows 10+)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE / fallback
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
                if res == 0:
                    break
        except Exception:
            pass

    def _theme_widget(self, w):
        c = self.c
        role = getattr(w, "_color_role", None)
        try:
            cls = w.winfo_class()
            if cls in ("Button", "Menubutton"):
                w.config(bg=c["btn_bg"], fg=c["btn_fg"],
                         activebackground=c["btn_active"], activeforeground=c["btn_fg"],
                         disabledforeground=c["disabled"])
            elif cls == "Label":
                w.config(bg=c["bg"], fg=c.get(role, c["fg"]) if role else c["fg"])
            elif cls == "Entry":
                fg = c["placeholder"] if role == "placeholder" else c["entry_fg"]
                w.config(bg=c["entry_bg"], fg=fg, insertbackground=c["entry_fg"],
                         disabledbackground=c["panel"], disabledforeground=c["disabled"],
                         highlightbackground=c["panel"], highlightcolor=c["accent"])
            elif cls == "Text":
                w.config(bg=c["entry_bg"], fg=c["entry_fg"], insertbackground=c["entry_fg"])
            elif cls == "Listbox":
                w.config(bg=c["list_bg"], fg=c["list_fg"],
                         selectbackground=c["sel"], selectforeground=c["sel_fg"],
                         highlightbackground=c["panel"], highlightcolor=c["accent"])
            elif cls == "Canvas":
                w.config(bg=c["bg"])
            elif cls in ("Frame", "Labelframe"):
                w.config(bg=c["bg"])
            elif cls == "Panedwindow":
                w.config(bg=c["bg"])
            elif cls == "Toplevel":
                w.config(bg=c["bg"])
                self._set_title_bar(w, True)
        except Exception:
            pass

    def theme_children(self, parent):
        """Apply the current palette to a widget and all its descendants."""
        self._theme_widget(parent)
        for w in parent.winfo_children():
            self._theme_widget(w)
            self.theme_children(w)

    def apply_theme(self):
        c = self.c
        # default colors for native popup menus and combobox dropdown lists
        root = self.root
        for opt, val in (("*Menu.background", c["menu_bg"]),
                         ("*Menu.foreground", c["menu_fg"]),
                         ("*Menu.activeBackground", c["btn_active"]),
                         ("*Menu.activeForeground", c["btn_fg"]),
                         ("*Menu.selectColor", c["sel"]),
                         ("*TCombobox*Listbox.background", c["list_bg"]),
                         ("*TCombobox*Listbox.foreground", c["list_fg"]),
                         ("*TCombobox*Listbox.selectBackground", c["sel"]),
                         ("*TCombobox*Listbox.selectForeground", c["sel_fg"])):
            try:
                root.option_add(opt, val)
            except Exception:
                pass
        style = self.ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=c["bg"], foreground=c["fg"],
                        fieldbackground=c["entry_bg"], bordercolor=c["border"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TButton", background=c["btn_bg"], foreground=c["btn_fg"],
                        lightcolor=c["btn_bg"], darkcolor=c["btn_bg"],
                        bordercolor=c["border"], padding=4)
        style.map("TButton",
                  background=[("active", c["btn_active"]), ("pressed", c["btn_active"])],
                  foreground=[("disabled", c["disabled"])])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                        indicatorbackground=c["entry_bg"], indicatorforeground=c["fg"],
                        bordercolor=c["border"])
        style.map("TCheckbutton",
                  background=[("active", c["bg"]), ("pressed", c["bg"])],
                  foreground=[("disabled", c["disabled"])])
        style.configure("TEntry", fieldbackground=c["entry_bg"], foreground=c["entry_fg"],
                        insertcolor=c["entry_fg"])
        style.configure("TCombobox", fieldbackground=c["entry_bg"], background=c["btn_bg"],
                        foreground=c["entry_fg"], arrowcolor=c["btn_fg"],
                        selectbackground=c["sel"], selectforeground=c["sel_fg"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["btn_bg"])],
                  foreground=[("readonly", c["btn_fg"])],
                  selectbackground=[("readonly", c["btn_bg"])],
                  selectforeground=[("readonly", c["btn_fg"])])
        style.configure("Treeview", background=c["tree_bg"], fieldbackground=c["tree_bg"],
                        foreground=c["tree_fg"], rowheight=self._rowheight,
                        font=self._data_font)
        style.map("Treeview",
                  background=[("selected", c["sel"])],
                  foreground=[("selected", c["sel_fg"])])
        style.configure("Treeview.Heading", background=c["panel"], foreground=c["label_key"],
                        relief="flat", font=self._data_font)
        style.map("Treeview.Heading", background=[("active", c["btn_active"])])

        # Relation tree: bigger rows + larger font so the custom "▼ / ▶"
        # fold indicator is easy to see and click.
        style.configure("Rel.Treeview", background=c["tree_bg"],
                        fieldbackground=c["tree_bg"], foreground=c["tree_fg"],
                        rowheight=self._rel_rowheight, font=self._rel_font)
        style.map("Rel.Treeview",
                  background=[("selected", c["sel"])],
                  foreground=[("selected", c["sel_fg"])])

        # Unit browser tree: dark background, large text + tall rows.
        style.configure("Unit.Treeview", background=c["tree_bg"],
                        fieldbackground=c["tree_bg"], foreground=c["tree_fg"],
                        rowheight=self._unit_rowheight, font=self._unit_font)
        style.map("Unit.Treeview",
                  background=[("selected", c["sel"])],
                  foreground=[("selected", c["sel_fg"])])

        # Notebook (unit browser / table grid tabs): dark tabs + dark body.
        style.configure("TNotebook", background=c["bg"], borderwidth=0,
                        tabmargins=(0, 4, 0, 0))
        style.configure("TNotebook.Tab", background=c["btn_bg"], foreground=c["btn_fg"],
                        padding=(16, 8), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", c["tree_bg"]), ("active", c["btn_active"])],
                  foreground=[("selected", c["fg"])])

        # Hide the tiny built-in ttk indicator; the relation tree draws its
        # own large "▼ / ▶" marker in the item text instead.
        if self._blank_indicator_img is None:
            img = self.tk.PhotoImage(width=1, height=1)
            img.transparency_set(0, 0, True)
            self._blank_indicator_img = img
        try:
            self.root.tk.call("ttk::style", "element", "create",
                              "Treeitem.indicator", "image",
                              self._blank_indicator_img, "-width", 1, "-height", 1)
        except Exception:
            pass
        style.configure("TScrollbar", background=c["btn_bg"], troughcolor=c["panel"],
                        bordercolor=c["panel"], arrowcolor=c["btn_fg"])
        style.map("TScrollbar", background=[("active", c["btn_active"])])
        style.configure("TProgressbar", background=c["accent"], troughcolor=c["panel"])
        for m in self._menus:
            try:
                m.config(bg=c["menu_bg"], fg=c["menu_fg"],
                         activebackground=c["btn_active"], activeforeground=c["btn_fg"])
            except Exception:
                pass
        self.root.config(bg=c["bg"])
        # unit browser canvas background + card colors follow the theme
        try:
            self.unit_canvas.configure(bg=c["bg"])
            if self.tables:
                self._build_unit_browser()
        except Exception:
            pass
        self._set_title_bar(self.root, True)
        # the immersive dark title bar only takes effect once the window is
        # really shown, so (re)apply it shortly after mapping
        self.root.after(200, lambda: self._set_title_bar(self.root, True))
        self.root.bind("<Map>", lambda e: self._set_title_bar(self.root, True))
        self.theme_children(self.root)
        for d in self.details:
            self.theme_children(d.fields_inner)
            self.theme_children(d.mini_inner)
        try:
            self.toolbar.config(highlightbackground=c["border"])
            self.status.config(highlightbackground=c["border"])
        except Exception:
            pass

    # ---------------- 素材导入 ----------------

    def open_asset_import(self):
        """打开 .bamod 素材包导入对话框（模型/图标/肖像合并进游戏 bundle + CRC）。"""
        import mod_assets
        mod_assets.AssetImportDialog(self.root)

    def open_texture_pack(self):
        """打开图标/肖像打包对话框（PNG -> .bamod，高级用法）。"""
        import mod_assets
        mod_assets.TexturePackDialog(self.root)

    def open_image_import(self):
        """打开图片导入对话框（图标/肖像等直接导入游戏 + 自定义容器路径与映射地址）。"""
        import mod_assets
        mod_assets.ImageImportDialog(self.root)

    def open_audio_import(self):
        """打开音频导入对话框（FMOD 音库文件级：列表/备份/替换/还原/批量）。"""
        import audio_import
        audio_import.AudioImportDialog(self.root)

    def open_audio_add(self):
        """打开添加音频/音效对话框（新事件注入音库 + 可选武器音效预设）。"""
        import audio_add
        audio_add.AudioAddDialog(self.root)

    # ---------------- language ----------------

    def _apply_lang(self):
        tr = self.tr
        tk = self.tk
        self.root.title(app_title_text(tr))
        bar = tk.Menu(self.root)
        self._menus = []
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label=tr.t("open_dump"), command=self.open_dump_dialog, accelerator="Ctrl+O")
        file_menu.add_command(label=tr.t("open_folder"), command=self.open_folder_dialog, accelerator="Ctrl+Shift+O")
        file_menu.add_separator()
        file_menu.add_command(label=tr.t("open_unity3d"), command=self.open_from_unity3d)
        file_menu.add_command(label=tr.t("export_unity3d"), command=self.export_from_unity3d)
        file_menu.add_command(label=tr.t("import_unity3d"), command=self.import_into_unity3d)
        file_menu.add_separator()
        file_menu.add_command(label="导入 .bamod 素材包…", command=self.open_asset_import)
        file_menu.add_command(label="导入图片/图标/肖像…", command=self.open_image_import)
        file_menu.add_command(label="打包图标/肖像 (.bamod)…", command=self.open_texture_pack)
        file_menu.add_command(label="导入音频/音效…", command=self.open_audio_import)
        file_menu.add_command(label="添加音频/音效（新音效）…", command=self.open_audio_add)
        file_menu.add_separator()
        file_menu.add_command(label=tr.t("save"), command=self.save, accelerator="Ctrl+S")
        file_menu.add_command(label=tr.t("save_as"), command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label=tr.t("export_folder"), command=self.export_folder_dialog)
        file_menu.add_separator()
        file_menu.add_command(label=tr.t("exit"), command=self.on_close)
        bar.add_cascade(label=tr.t("menu_file"), menu=file_menu)
        edit_menu = tk.Menu(bar, tearoff=0)
        edit_menu.add_command(label=tr.t("undo"), command=self.do_undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label=tr.t("redo"), command=self.do_redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label=tr.t("add_row"), command=self.add_row)
        edit_menu.add_command(label=tr.t("duplicate_row"), command=self.duplicate_row)
        edit_menu.add_command(label=tr.t("delete_row"), command=self.delete_row, accelerator="Del")
        bar.add_cascade(label=tr.t("menu_edit"), menu=edit_menu)
        tools_menu = tk.Menu(bar, tearoff=0)
        tools_menu.add_command(label=tr.t("lookup_title"), command=self._focus_lookup, accelerator="F3")
        tools_menu.add_command(label=tr.t("find_references"), command=self.find_references_dialog)
        tools_menu.add_command(label=tr.t("validate"), command=self.validate_dialog, accelerator="F7")
        bar.add_cascade(label=tr.t("menu_tools"), menu=tools_menu)
        lang_menu = tk.Menu(bar, tearoff=0)
        for code in LANGS:
            label = LANG_NAMES[code] + ("  ✓" if code == self.tr.lang else "")
            lang_menu.add_command(label=label, command=lambda c=code: self.set_language(c))
        bar.add_cascade(label=tr.t("menu_language"), menu=lang_menu)
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label=tr.t("help_guide"), command=self.show_help, accelerator="F1")
        help_menu.add_command(label=tr.t("about"), command=self.show_about)
        bar.add_cascade(label=tr.t("menu_help"), menu=help_menu)
        self._menus += [file_menu, edit_menu, tools_menu, lang_menu, help_menu]
        # draw our own menu strip (the native strip cannot be recolored on
        # Windows); buttons open their menu via tk_popup, which is reliable
        for w in self.menubar_frame.winfo_children():
            w.destroy()
        for mlabel, mmenu in ((tr.t("menu_file"), file_menu),
                              (tr.t("menu_edit"), edit_menu),
                              (tr.t("menu_tools"), tools_menu),
                              (tr.t("menu_language"), lang_menu),
                              (tr.t("menu_help"), help_menu)):
            mb = tk.Button(self.menubar_frame, text=mlabel,
                           relief="flat", padx=14, pady=4,
                           bg=self.c["btn_bg"], fg=self.c["btn_fg"],
                           activebackground=self.c["btn_active"],
                           activeforeground=self.c["btn_fg"],
                           highlightbackground=self.c["border"],
                           command=lambda menu=mmenu: self._post_menu(menu))
            mb.pack(side="left")
            self._menu_buttons = getattr(self, "_menu_buttons", []) + [mb]
        self.theme_children(self.menubar_frame)
        labels = {
            "open": tr.t("btn_open"),
            "import_unity3d": tr.t("btn_import_unity3d"),
            "save": tr.t("btn_save"),
            "unity3d": tr.t("btn_unity3d"),
            "undo": tr.t("btn_undo"),
            "redo": tr.t("btn_redo"),
            "add": tr.t("btn_add"),
            "dup": tr.t("btn_dup"),
            "del": tr.t("btn_del"),
            "refs": tr.t("btn_refs"),
            "validate": tr.t("btn_validate"),
        }
        for key, b in self.tool_buttons.items():
            text = labels.get(key, key)
            code, char = EMOJI.get(key, (None, None))
            img = icon_image(self.root, code) if code else None
            if img is not None:
                self._icon_refs[key] = img
                b.config(image=img, compound="left", text=text)
            elif char:
                b.config(image="", compound="none", text=char + " " + text)
            else:
                b.config(image="", compound="none", text=text)
        self._icon_text(self.lang_btn, "lang", LANG_NAMES[self.tr.lang])
        self._icon_text(self.dict_btn, "dict", tr.t("btn_dict"))
        self._icon_text(self.table_label_w, "table", tr.t("table_label"))
        self._icon_text(self.search_label_w, "search", tr.t("filter_label"))
        self._icon_text(self.lookup_label_w, "lookup", tr.t("btn_lookup") + ":")
        # unit browser tab labels + rebuilt hierarchy in the new language
        try:
            self.main_notebook.tab(0, text=tr.t("tab_units"))
            self.main_notebook.tab(1, text=tr.t("tab_tables"))
        except Exception:
            pass
        self._build_unit_browser()
        # re-render the table grid so column headings AND formatted cell
        # values (localized UIName / FK names / enum meanings) follow the new
        # language; the fmt cache is invalidated by _fmt_lang
        if self.current_table and self.current_table in self.tables:
            self.rebuild_tree()
        # refresh the column-filter combo so its "all columns" entry follows
        # the language (column names themselves are not translated)
        if self.current_table:
            cols = self._columns()
            self.col_combo.config(values=[tr.t("pick_column")] + cols)
            self.col_combo.set(self.filter_column if self.filter_column in cols
                               else tr.t("pick_column"))
        else:
            self.col_combo.config(values=[tr.t("pick_column")])
            self.col_combo.set(tr.t("pick_column"))
        self._update_status()
        for d in self.details:
            d.apply_lang()
        # refresh the dictionary window in the new language if it is open
        if getattr(self, "_dict_render", None) is not None and getattr(self, "_dict_win", None) is not None:
            try:
                if self._dict_win.winfo_exists():
                    self._dict_render()
                    if getattr(self, "_model_render", None) is not None:
                        self._model_render()
                    nb = getattr(self, "_dict_notebook", None)
                    if nb is not None:
                        nb.tab(0, text=self.tr.t("dict_title"))
                        nb.tab(1, text=self.tr.t("dict_tab_models"))
            except Exception:
                pass

    def _icon_text(self, widget, emoji_key, text):
        """Set a label/button with a colored icon + text (fallback: emoji char)."""
        code, char = EMOJI.get(emoji_key, (None, None))
        img = icon_image(self.root, code) if code else None
        if img is not None:
            self._icon_refs[emoji_key] = img
            widget.config(image=img, compound="left", text=text)
        elif char:
            widget.config(image="", compound="none", text=char + " " + text)
        else:
            widget.config(image="", compound="none", text=text)

    def _post_menu(self, menu):
        """Open a menu under the pointer (reliable on Windows)."""
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def set_language(self, code):
        if code not in LANGS:
            return
        self.tr.set(code)
        settings = load_settings()
        settings["language"] = code
        save_settings(settings)
        self._apply_lang()
        self._toast(self.tr.t("language_switched", lang=LANG_NAMES[code]))

    def cycle_language(self):
        i = LANGS.index(self.tr.lang)
        self.set_language(LANGS[(i + 1) % len(LANGS)])

    # ---------------- status / toast ----------------

    def _toast(self, text):
        """Transient status-bar message (replaces popups for routine info)."""
        self.toast_l.config(text=text)
        if self._toast_after is not None:
            self.root.after_cancel(self._toast_after)
        self._toast_after = self.root.after(5000, lambda: self.toast_l.config(text=""))

    def _update_status(self):
        tr = self.tr
        fname = tr.t("status_none")
        if self.dump_path:
            fname = os.path.basename(self.dump_path)
        elif self.folder_path:
            fname = os.path.basename(self.folder_path.rstrip("\\/")) + "/"
        self.status_file_l.config(text=tr.t("status_file") + ": " + fname)
        self.status_dirty_l.config(text=tr.t("status_dirty") if self.dirty else tr.t("status_clean"))
        total = len(self.filtered_map)
        rows = len(self.tables.get(self.current_table, [])) if self.current_table else 0
        self.status_rows_l.config(text=tr.t("status_rows", shown=total, total=rows))
        self.status_lang_l.config(text=LANG_NAMES[self.tr.lang])
        self.tool_buttons["undo"].config(state="normal" if self.undo.can_undo() else "disabled")
        self.tool_buttons["redo"].config(state="normal" if self.undo.can_redo() else "disabled")

    def _mark_dirty(self):
        self.dirty = True
        self._data_version += 1
        self._update_status()

    # ---------------- open ----------------

    def open_dump_dialog(self):
        if not self._confirm_discard():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self.tr.t("open_dump"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.open_dump(path)

    def open_dump(self, path, unity3d_path=None):
        try:
            obj = load_dump(path)
        except Exception as e:
            self._show_error(self.tr.t("msg_bad_dump", error=e))
            return
        if not is_dump_obj(obj):
            self._show_error(self.tr.t("msg_bad_dump", error="no DataBaseCompiled fields found"))
            return
        self._run_async(
            label=self.tr.t("msg_decrypting", name=os.path.basename(path)),
            func=lambda: decrypt_tables(obj),
            on_done=lambda res: self._finish_open_dump(path, obj, res, unity3d_path))

    def _finish_open_dump(self, path, obj, res, unity3d_path=None):
        tables, errors = res
        if not tables:
            self._show_error(self.tr.t("msg_bad_dump", error="no tables decrypted"))
            return
        self.commit_editor(silent=True)
        self.close_detail()
        self.tables = tables
        self.dump_obj = obj
        self.dump_path = path
        self.folder_path = None
        self.source_is_dump = True
        self.unity3d_path = unity3d_path
        self.dirty = False
        self._data_version += 1
        self.undo.clear()
        self._select_table(None)
        self._refresh_table_list()
        self._build_unit_browser()
        try:
            self.main_notebook.select(0)  # show the big unit browser first
        except Exception:
            pass
        self._update_status()
        self._toast(self.tr.t("msg_loaded", name=os.path.basename(path), tables=len(tables)))
        if errors:
            self._show_warning(self.tr.t("msg_decrypt_errors",
                                         errors="\n".join(f"{k}: {e}" for k, e in errors)))

    def open_folder_dialog(self):
        if not self._confirm_discard():
            return
        from tkinter import filedialog
        folder = filedialog.askdirectory(title=self.tr.t("open_folder"))
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder):
        try:
            tables, missing = import_folder(folder)
        except Exception as e:
            self._show_error(self.tr.t("msg_bad_dump", error=e))
            return
        if not tables:
            self._show_error(self.tr.t("msg_bad_dump", error="no table files found"))
            return
        self.commit_editor(silent=True)
        self.close_detail()
        self.tables = tables
        self.folder_path = folder
        self.dump_obj = None
        self.dump_path = None
        self.source_is_dump = False
        self.unity3d_path = None
        self.dirty = False
        self._data_version += 1
        self.undo.clear()
        self._select_table(None)
        self._refresh_table_list()
        self._build_unit_browser()
        try:
            self.main_notebook.select(0)  # show the big unit browser first
        except Exception:
            pass
        self._update_status()
        self._toast(self.tr.t("msg_folder_loaded", tables=len(tables), name=folder))
        if missing:
            self._show_warning(self.tr.t("msg_missing_tables", missing=", ".join(missing)))


    # ---------------- table list / tree ----------------

    def _refresh_table_list(self):
        self.table_list.delete(0, "end")
        names = [table_name(f) for f in TABLE_FIELDS]
        for n in names:
            count = len(self.tables.get(n, []))
            self.table_list.insert("end", f"{n}  ({count})")
        if self.current_table and self.current_table in names:
            self.table_list.selection_set(names.index(self.current_table))

    # ---------------- unit browser (country -> spec -> branch -> unit) ----------------

    def _build_unit_browser(self):
        """Draw the card/panel unit browser: 国家 -> 专精 -> 兵种 -> 单位,
        with a collapsible fold bar on every section (▼ expand / ▶ collapse,
        collapsed by default) and a highlight on the last-clicked bar."""
        canvas = self.unit_canvas
        canvas.delete("all")
        self._unit_icons = {}
        self._unit_cards = {}
        if not hasattr(self, "_unit_folds"):
            self._unit_folds = {}
        folds = self._unit_folds
        active = getattr(self, "_unit_active_fold", None)
        if "Units" not in self.tables or "Specializations" not in self.tables:
            canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        lang = self.tr.lang
        c = self.c
        fam = getattr(self, "_cjk_font_family", None) or ""
        W = max(canvas.winfo_width(), 680)

        PAD = 10
        CARD_H = 72
        CARD_GAP = 6
        SPEC_H = 60
        COUNTRY_H = 70
        CAT_H = 42
        ICON_W = 220   # uniform unit-icon display box (equal width/height)
        ICON_H = 52
        x0 = PAD
        x1 = W - PAD

        SPECIAL_NAME = {"zh": "特殊", "en": "Special", "ru": "Особые"}
        special_name = SPECIAL_NAME.get(lang, "Special")

        def fkey(kind, *parts):
            return "%s:%s" % (kind, ":".join(str(p) for p in parts))

        def is_special(u):
            """Units never shown in the game armory / never meant for a deck:
            DisplayInArmory=false, NPC models (e.g. C-17 Globemaster (takeoff))
            and training-target drones (US_Trick*, e.g. the Mi-24 training
            target must not appear under USMC or any other spec)."""
            if u.get("DisplayInArmory") is False:
                return True
            model = u.get("ModelFileName")
            if isinstance(model, str):
                if model.startswith("NPC_") or model.startswith("US_Trick"):
                    return True
            return False

        tables = self.tables
        countries = {cc["Id"]: cc for cc in tables.get("Countries", [])
                     if isinstance(cc, dict) and "Id" in cc}
        specs = [s for s in tables.get("Specializations", [])
                 if isinstance(s, dict) and "Id" in s]
        unit_by_id = {}
        for i, u in enumerate(tables.get("Units", [])):
            if isinstance(u, dict) and "Id" in u:
                unit_by_id[u["Id"]] = (i, u)
        # direct spec -> units from SpecializationAvailabilities
        avail = {}
        sa_by_id = {}
        for a in tables.get("SpecializationAvailabilities", []):
            if isinstance(a, dict) and "Id" in a:
                sa_by_id[a["Id"]] = a
                avail.setdefault(a.get("SpecializationId"), set()).add(a.get("UnitId"))
        # transports: a vehicle is available to the specs of the infantry it
        # carries (TransportAvailabilities -> SpecializationAvailabilities).
        transport_spec_units = {}
        for t in tables.get("TransportAvailabilities", []):
            if not isinstance(t, dict):
                continue
            sa = sa_by_id.get(t.get("SpecializationAvailabilityId"))
            vid = t.get("UnitId")
            if sa is not None and isinstance(sa.get("SpecializationId"), int) \
                    and isinstance(vid, int):
                transport_spec_units.setdefault(sa["SpecializationId"], set()).add(vid)
        # "common" base units: in no availability table at all -> every spec
        covered = set()
        for s in avail.values():
            covered |= s
        for s in transport_spec_units.values():
            covered |= s
        common = set(unit_by_id.keys()) - covered

        y = 8

        def draw_cards(units_sorted):
            nonlocal y
            for idx, u in units_sorted:
                uname = u.get("HUDName") or u.get("Name") or "?"
                cost = u.get("Cost")
                cost_str = ("%d" % cost) if isinstance(cost, float) and cost.is_integer() \
                    else (str(cost) if isinstance(cost, (int, float)) else "")
                tag = "u%d" % idx
                canvas.create_rectangle(x0 + 8, y, x1, y + CARD_H,
                                        fill=c["entry_bg"], outline=c["border"], tags=(tag,))
                uimg = game_icon_image(self.root, u.get("ThumbnailFileName") or "", ICON_W, ICON_H)
                if uimg is not None:
                    iw, ih = uimg.width(), uimg.height()
                    ix = x0 + 12 + (ICON_W - iw) // 2
                    iy = y + CARD_H // 2 - ih // 2
                    iid = canvas.create_image(ix, iy, image=uimg, anchor="nw", tags=(tag,))
                    self._unit_icons[iid] = uimg
                canvas.create_text(x0 + 12 + ICON_W + 14, y + CARD_H // 2, anchor="w", text=uname,
                                   fill=c["fg"], font=(fam, 12), tags=(tag,))
                if cost_str:
                    canvas.create_text(x1 - 16, y + CARD_H // 2, anchor="e",
                                       text=cost_str, fill=c["hint"], font=(fam, 12), tags=(tag,))
                self._unit_cards[tag] = ("Units", idx)
                y += CARD_H + CARD_GAP

        for cid in sorted(countries):
            cc = countries[cid]
            if cc.get("Hidden"):
                continue
            cname = COUNTRY_NAMES.get(cc.get("Name"), {}).get(lang, cc.get("Name") or "?")
            ckey = fkey("country", cid)
            open_c = folds.get(ckey, False)
            is_act = ckey == active
            ctag = "fold:" + ckey
            canvas.create_rectangle(x0, y, x1, y + COUNTRY_H,
                                    fill=(c["sel"] if is_act else c["btn_active"]),
                                    outline=(c["accent"] if is_act else c["border"]),
                                    tags=(ctag,))
            cimg = game_icon_image(self.root, cc.get("FlagFileName") or "", 56, 56)
            if cimg is not None:
                iid = canvas.create_image(x0 + 10, y + COUNTRY_H // 2, image=cimg, anchor="w", tags=(ctag,))
                self._unit_icons[iid] = cimg
            canvas.create_text(x0 + 74, y + COUNTRY_H // 2, anchor="w",
                               text=("▼ " if open_c else "▶ ") + cname,
                               fill=c["fg"], font=(fam, 15, "bold"), tags=(ctag,))
            y += COUNTRY_H + 6
            if not open_c:
                continue

            country_specs = [s for s in specs
                             if s.get("CountryId") == cid and not s.get("Hidden")]
            for s in sorted(country_specs, key=lambda x: x.get("Id", 0)):
                sname = localize_text(s.get("UIName") or s.get("Name") or "", lang)
                if not sname:
                    sname = s.get("Name") or "?"
                skey = fkey("spec", s.get("Id"))
                open_s = folds.get(skey, False)
                is_act_s = skey == active
                stag = "fold:" + skey
                canvas.create_rectangle(x0 + 8, y, x1, y + SPEC_H,
                                        fill=(c["sel"] if is_act_s else c["btn_bg"]),
                                        outline=(c["accent"] if is_act_s else c["border"]),
                                        tags=(stag,))
                simg = game_icon_image(self.root, s.get("Icon") or "", 48, 48)
                if simg is not None:
                    iid = canvas.create_image(x0 + 44, y + SPEC_H // 2, image=simg, anchor="center", tags=(stag,))
                    self._unit_icons[iid] = simg
                canvas.create_text(x0 + 76, y + SPEC_H // 2, anchor="w",
                                   text=("▼ " if open_s else "▶ ") + sname,
                                   fill=c["fg"], font=(fam, 14, "bold"), tags=(stag,))
                y += SPEC_H + 4
                if not open_s:
                    continue

                spec_units = (set(avail.get(s.get("Id"), set()))
                             | transport_spec_units.get(s.get("Id"), set())
                             | common)
                bycat = {}
                for uid in spec_units:
                    item = unit_by_id.get(uid)
                    if not item:
                        continue
                    idx, u = item
                    if u.get("CountryId") != cid or u.get("IsUnitModification"):
                        continue
                    if is_special(u):
                        continue  # armory-invisible/NPC units -> top-level 特殊 bar
                    bycat.setdefault(u.get("CategoryType"), []).append((idx, u))

                for cat in sorted(bycat):
                    catname = CATEGORY_NAMES.get(cat, {}).get(lang, str(cat))
                    ckey2 = fkey("cat", cid, s.get("Id"), cat)
                    open_cat = folds.get(ckey2, False)
                    is_act_c = ckey2 == active
                    ctag2 = "fold:" + ckey2
                    canvas.create_rectangle(x0 + 8, y, x1, y + CAT_H,
                                            fill=(c["sel"] if is_act_c else c["btn_bg"]),
                                            outline=(c["accent"] if is_act_c else c["border"]),
                                            tags=(ctag2,))
                    canvas.create_text(x0 + 16, y + CAT_H // 2, anchor="w",
                                       text=("▼ " if open_cat else "▶ ") + catname,
                                       fill=(c["sel_fg"] if is_act_c else c["fg"]),
                                       font=(fam, 12, "bold"), tags=(ctag2,))
                    y += CAT_H + 2
                    if not open_cat:
                        continue
                    draw_cards(sorted(bycat[cat],
                                     key=lambda x: (x[1].get("Name") or "").lower()))
                y += 10

        # top-level "特殊" fold bar (parallel to countries): every unit that
        # is never shown in the game armory (DisplayInArmory = false)
        special_all = []
        for i, u in enumerate(tables.get("Units", [])):
            if not isinstance(u, dict) or u.get("IsUnitModification"):
                continue
            if not is_special(u):
                continue
            special_all.append((i, u))
        if special_all:
            skey = "special"
            open_sp = folds.get(skey, False)
            is_act_sp = skey == active
            stag = "fold:" + skey
            canvas.create_rectangle(x0, y, x1, y + COUNTRY_H,
                                    fill=(c["sel"] if is_act_sp else c["btn_active"]),
                                    outline=(c["accent"] if is_act_sp else c["border"]),
                                    tags=(stag,))
            canvas.create_text(x0 + 16, y + COUNTRY_H // 2, anchor="w",
                               text=("▼ " if open_sp else "▶ ") + special_name,
                               fill=c["fg"], font=(fam, 14, "bold"), tags=(stag,))
            y += COUNTRY_H + 6
            if open_sp:
                sp_bycat = {}
                for idx, u in special_all:
                    sp_bycat.setdefault(u.get("CategoryType"), []).append((idx, u))
                for cat in sorted(sp_bycat):
                    catname = CATEGORY_NAMES.get(cat, {}).get(lang, str(cat))
                    ckey4 = fkey("cat", "special", cat)
                    open_cat = folds.get(ckey4, False)
                    is_act_c = ckey4 == active
                    ctag4 = "fold:" + ckey4
                    canvas.create_rectangle(x0 + 8, y, x1, y + CAT_H,
                                            fill=(c["sel"] if is_act_c else c["btn_bg"]),
                                            outline=(c["accent"] if is_act_c else c["border"]),
                                            tags=(ctag4,))
                    canvas.create_text(x0 + 16, y + CAT_H // 2, anchor="w",
                                       text=("▼ " if open_cat else "▶ ") + catname,
                                       fill=(c["sel_fg"] if is_act_c else c["fg"]),
                                       font=(fam, 12, "bold"), tags=(ctag4,))
                    y += CAT_H + 2
                    if open_cat:
                        draw_cards(sorted(sp_bycat[cat],
                                         key=lambda x: (x[1].get("Name") or "").lower()))

        canvas.configure(scrollregion=(0, 0, W, y))

    def _on_unit_canvas_resize(self, _event=None):
        if not self.tables:
            return
        if getattr(self, "_unit_redraw_after", None):
            try:
                self.root.after_cancel(self._unit_redraw_after)
            except Exception:
                pass
        self._unit_redraw_after = self.root.after(150, self._build_unit_browser)

    def _unit_card_at(self, event):
        item = self.unit_canvas.find_withtag("current")
        if not item:
            return None
        for t in self.unit_canvas.gettags(item[0]):
            if t.startswith("u") and t in self._unit_cards:
                return self._unit_cards[t]
        return None

    def on_unit_click(self, event):
        item = self.unit_canvas.find_withtag("current")
        if not item:
            self._unit_selected = None
            return
        tags = self.unit_canvas.gettags(item[0])
        for t in tags:
            if t.startswith("fold:"):
                key = t[len("fold:"):]
                self._unit_folds[key] = not self._unit_folds.get(key, False)
                self._unit_active_fold = key
                self._build_unit_browser()
                return
        for t in tags:
            if t.startswith("u"):
                rec = self._unit_cards.get(t)
                self._unit_selected = rec if rec else None
                self._unit_selected_idx = int(t[1:]) if t[1:].isdigit() else None
                self._highlight_selected_card()
                return
        self._unit_selected = None
        self._unit_selected_idx = None
        self._highlight_selected_card()

    def _highlight_selected_card(self):
        """Recolor card bodies so the selected unit card stands out."""
        c = self.c
        sel = getattr(self, "_unit_selected_idx", None)
        for tag in self._unit_cards:
            items = self.unit_canvas.find_withtag(tag)
            for it in items:
                if self.unit_canvas.type(it) != "rectangle":
                    continue
                if sel is not None and tag == "u%d" % sel:
                    self.unit_canvas.itemconfig(it, fill=c["sel"], outline=c["accent"])
                else:
                    self.unit_canvas.itemconfig(it, fill=c["entry_bg"], outline=c["border"])

    def on_unit_double_click(self, event):
        rec = self._unit_card_at(event)
        if rec:
            self.open_detail(rec[0], rec[1])

    def on_table_select(self, _event=None):
        sel = self.table_list.curselection()
        if not sel:
            return
        name = self.table_list.get(sel[0]).split("  (")[0]
        if name != self.current_table:
            if not self.commit_editor(silent=False):
                names = [table_name(f) for f in TABLE_FIELDS]
                if self.current_table in names:
                    self.table_list.selection_clear(0, "end")
                    self.table_list.selection_set(names.index(self.current_table))
                return
            self._select_table(name)

    def _select_table(self, name):
        # show the raw table grid tab when the user picks a table
        try:
            self.main_notebook.select(1)
        except Exception:
            pass
        self.commit_editor(silent=True)
        self.current_table = name
        self.current_row = None
        self.filter_text = ""
        self.filter_column = None
        self.search_var.set("")
        names = [table_name(f) for f in TABLE_FIELDS]
        if name and name in self.tables and name in names:
            idx = names.index(name)
            self.table_list.selection_clear(0, "end")
            self.table_list.selection_set(idx)
            self.table_list.see(idx)
        elif not name:
            for n in names:
                if n in self.tables:
                    self.current_table = n
                    idx = names.index(n)
                    self.table_list.selection_clear(0, "end")
                    self.table_list.selection_set(idx)
                    break
            else:
                self.current_table = None
        self.rebuild_tree()

    def _table_columns(self, table):
        """Key columns of a given table (grid view), cached."""
        if self._cols_ver != self._data_version or table not in self._cols_cache:
            rows = self.tables.get(table, [])
            if table in KEY_COLUMNS:
                cols = [c for c in KEY_COLUMNS[table]
                        if any(isinstance(r, dict) and c in r for r in rows)]
                if "Id" not in cols and any(isinstance(r, dict) and "Id" in r for r in rows):
                    cols.insert(0, "Id")
            else:
                cols = []
                for r in rows:
                    if isinstance(r, dict):
                        for k in r.keys():
                            if k not in cols:
                                cols.append(k)
            if not cols:
                cols = ["Id"]
            self._cols_cache[table] = cols
            self._cols_ver = self._data_version
        return self._cols_cache[table]

    def _columns(self):
        if self.current_table is None:
            return ["Id"]
        return self._table_columns(self.current_table)

    def _fk_display(self, field, val):
        """Resolve an FK id (WeaponId / UnitId / ArmorId / ...) to the
        referenced row's name: e.g. 7 -> 'M249 SAW (7)'. 0 and null stay as is.

        TransportAvailabilities.SpecializationAvailabilityId points to a
        SpecializationAvailabilities row whose meaning is "unit X is offered
        in this specialization" — display that UNIT (e.g. 104 -> 'Force
        Recon (61)') so the field reads as the carried squad."""
        target = FIELD_REF_MAP.get(field)
        if not target or target not in self.tables or not isinstance(val, int) or val <= 0:
            return fmt_cell(val)
        if field == "SpecializationAvailabilityId" and target == "SpecializationAvailabilities":
            return self._fk_name_for(field, target, val)
        return f"{self._row_name(target, val)} ({val})"

    def _enum_cell(self, table, field, val, mark="➜"):
        """Enum cell display: '11 ➜ 主战坦克 (Tank)' — the arrow points at the
        kind the raw ID stands for (Units.Type / CategoryType / Role,
        Weapons.Type, Ammunitions.TrajectoryType, ...).

        Returns None when the field is not an enum field, so callers fall back
        to the plain formatter. Unknown values keep the raw number and are
        flagged with a warning arrow (they are legal — the game just takes the
        fallback branch)."""
        key = enum_key(table or "", field)
        if key is None or isinstance(val, bool) or not isinstance(val, int):
            return None
        if val < 0:  # e.g. ContentMembership -1 = base game
            label = enum_label(key, val, self.tr.lang)
            if not label:
                return fmt_cell(val)
            return f"{val} {mark} {label}"
        label = enum_label(key, val, self.tr.lang)
        if label:
            return f"{val} {mark} {label}"
        return f"{val} {mark} ⚠ {self.tr.t('enum_unknown', value=val)}"

    def _fmt_row(self, row, cols, table=None):
        out = []
        for c in cols:
            v = row.get(c)
            enum_text = self._enum_cell(table, c, v) if table else None
            if enum_text is not None:
                out.append(enum_text)
            elif c in FIELD_REF_MAP:
                out.append(self._fk_display(c, v))
            elif c == "UIName" and isinstance(v, str) and v.strip():
                out.append(localize_text(v, self.tr.lang))
            else:
                out.append(fmt_cell(v))
        return tuple(out)

    def _fmt_for_table(self, table):
        """Cached formatted cell values for a table (localized, so the cache
        is keyed by data version AND the current UI language)."""
        rows = self.tables.get(table, [])
        if (self._fmt_ver.get(table) != self._data_version
                or self._fmt_lang != self.tr.lang):
            self._ensure_indexes()
            cols = self._table_columns(table)
            fmts = []
            for r in rows:
                if isinstance(r, dict):
                    fmts.append(self._fmt_row(r, cols, table))
                else:
                    fmts.append(())
            self._fmt_cache[table] = fmts
            self._fmt_ver[table] = self._data_version
            self._fmt_lang = self.tr.lang
        return self._fmt_cache[table]

    def rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        if not self.current_table:
            self.col_combo.config(values=[self.tr.t("pick_column")])
            self.col_combo.set(self.tr.t("pick_column"))
            self._update_status()
            return
        cols = self._columns()
        self.tree.config(columns=cols)
        for c in cols:
            if c == "Id":
                w = 60
            elif "Name" in c or "HUDName" in c or c in FIELD_REF_MAP:
                w = 200
            else:
                w = 110
            self.tree.heading(c, text=self._field_label(self.current_table, c))
            self.tree.column(c, width=w, minwidth=40, stretch=False)
        allcols = [self.tr.t("pick_column")] + cols
        self.col_combo.config(values=allcols)
        if self.filter_column not in cols:
            self.filter_column = None
        self.col_combo.set(self.filter_column if self.filter_column else self.tr.t("pick_column"))
        self.apply_filter()
        self._update_status()

    def schedule_filter(self, _event=None):
        """Debounced search: rebuild the view 250 ms after typing stops."""
        if self._search_after is not None:
            self.root.after_cancel(self._search_after)
        self._search_after = self.root.after(250, self._apply_filter_debounced)

    def _apply_filter_debounced(self):
        self._search_after = None
        self.apply_filter()

    def apply_filter(self):
        if not self.current_table:
            return
        table = self.current_table
        text = self.search_var.get().strip().lower()
        self.filter_text = text
        colsel = self.col_combo.get()
        self.filter_column = None if colsel in ("", self.tr.t("pick_column")) else colsel

        rows = self.tables.get(table, [])
        fmts = self._fmt_for_table(table)
        cols = self._table_columns(table)
        col_idx = cols.index(self.filter_column) if self.filter_column in cols else -1

        matched = []
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            if text:
                if col_idx >= 0:
                    hit = text in fmts[i][col_idx].lower()
                else:
                    hit = text in "\x1f".join(fmts[i]).lower()
                if not hit:
                    continue
            matched.append(i)

        if self._table_has_order(table):
            matched = self._sort_by_order_id(table, matched)

        gen = self._fill_gen + 1
        self._fill_gen = gen
        self.tree.delete(*self.tree.get_children())
        self.filtered_map = []

        def fill_chunk(start):
            if gen != self._fill_gen or self.current_table is None:
                return
            end = min(start + TREE_CHUNK, len(matched))
            tree = self.tree
            for i in range(start, end):
                ri = matched[i]
                tree.insert("", "end", values=fmts[ri])
                self.filtered_map.append((table, ri))
            if end < len(matched):
                self.root.after(10, lambda: fill_chunk(end))
            else:
                self._update_status()

        if len(matched) <= TREE_CHUNK:
            for ri in matched:
                self.tree.insert("", "end", values=fmts[ri])
                self.filtered_map.append((table, ri))
            self._update_status()
        else:
            self._update_status()
            self.root.after(10, lambda: fill_chunk(0))

    def clear_filter(self):
        self.search_var.set("")
        self.filter_column = None
        self.col_combo.set(self.tr.t("pick_column"))
        self.apply_filter()

    def on_row_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            pos = self.tree.index(sel[0])
        except Exception:
            return
        if 0 <= pos < len(self.filtered_map):
            table, idx = self.filtered_map[pos]
            self.current_row = idx if table == self.current_table else None

    def on_row_double_click(self, _event=None):
        if self.current_row is None:
            return
        self.open_detail(self.current_table, self.current_row)

    def refresh_visible_row(self, row_idx=None):
        """Update a row's values in the main grid, if visible."""
        children = self.tree.get_children()
        for pos, (table, idx) in enumerate(self.filtered_map):
            if table == self.current_table and pos < len(children) and \
                    (row_idx is None and idx == self.current_row or idx == row_idx):
                row = self.tables[table][idx]
                cols = self._columns()
                self.tree.item(children[pos], values=self._fmt_row(row, cols, table))
                if row_idx is not None:
                    break

    # ---------------- row ops ----------------

    def add_row(self):
        if not self.current_table:
            return
        self.commit_editor(silent=False)
        rows = self.tables[self.current_table]
        rid = next_free_id(rows)
        new_row = self._blank_row(self.current_table)
        new_row["Id"] = rid
        old = list(rows)
        self.undo.push(self.current_table, old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        self.rebuild_tree()
        self.current_row = len(rows) - 1
        self._select_row_in_tree()
        self.open_detail(self.current_table, self.current_row)
        self._toast(self.tr.t("new_id_allocated", id=rid))
        # 新武器：自动附加最常用的弹药
        if self.current_table == "Weapons":
            self._auto_add_common_ammo(rid)

    def duplicate_table_row(self, table, idx):
        """Copy a row into a new row (new Id) in the same table.
        Returns the new row index, or None."""
        if table not in self.tables:
            return None
        rows = self.tables[table]
        if not (0 <= idx < len(rows)) or not isinstance(rows[idx], dict):
            return None
        new_row = json.loads(json.dumps(rows[idx]))
        new_row["Id"] = next_free_id(rows)
        old = list(rows)
        self.undo.push(table, old, old + [new_row], "duplicate")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if table == self.current_table:
            self.rebuild_tree()
        return len(rows) - 1

    def duplicate_row(self):
        if self.current_row is None or self.current_table not in self.tables:
            return
        self.commit_editor(silent=False)
        new_idx = self.duplicate_table_row(self.current_table, self.current_row)
        if new_idx is None:
            return
        self.current_row = new_idx
        self._select_row_in_tree()
        self.open_detail(self.current_table, self.current_row)
        self._toast(self.tr.t("new_id_allocated",
                              id=self.tables[self.current_table][new_idx].get("Id")))

    def delete_table_row(self, table, idx):
        """Silently delete one row. Returns True on success."""
        if table not in self.tables:
            return False
        rows = self.tables[table]
        if not (0 <= idx < len(rows)):
            return False
        old = list(rows)
        self.undo.push(table, old, old[:idx] + old[idx+1:], "delete")
        del rows[idx]
        self._mark_dirty()
        self._refresh_table_list()
        if table == self.current_table:
            self.rebuild_tree()
        return True

    def delete_row(self):
        if self.current_row is None or self.current_table not in self.tables:
            return
        if not self.delete_table_row(self.current_table, self.current_row):
            return
        self.current_row = None
        self._refresh_detail()

    def _select_row_in_tree(self):
        for pos, (table, idx) in enumerate(self.filtered_map):
            if table == self.current_table and idx == self.current_row:
                children = self.tree.get_children()
                if pos < len(children):
                    self.tree.selection_set(children[pos])
                    self.tree.see(children[pos])
                return
        self.search_var.set("")
        self.apply_filter()
        for pos, (table, idx) in enumerate(self.filtered_map):
            if table == self.current_table and idx == self.current_row:
                children = self.tree.get_children()
                if pos < len(children):
                    self.tree.selection_set(children[pos])
                    self.tree.see(children[pos])
                return

    # ---------------- undo / redo ----------------

    def do_undo(self):
        self.commit_editor(silent=False)
        table = self.undo.undo(self.tables)
        if table:
            self._mark_dirty()
            self.current_row = None
            if table == self.current_table:
                self.rebuild_tree()
            else:
                self._refresh_table_list()
            self._refresh_detail()
            self._update_status()

    def do_redo(self):
        self.commit_editor(silent=False)
        table = self.undo.redo(self.tables)
        if table:
            self._mark_dirty()
            self.current_row = None
            if table == self.current_table:
                self.rebuild_tree()
            else:
                self._refresh_table_list()
            self._refresh_detail()
            self._update_status()

    # ---------------- save ----------------

    def save(self):
        if not self.tables:
            self._show_warning(self.tr.t("msg_no_file"))
            return
        self.commit_editor(silent=False)
        if self.unity3d_path:
            self._write_to_unity3d(self.unity3d_path)
        elif self.source_is_dump and self.dump_obj is not None:
            self._save_dump(self.dump_path)
        else:
            self._save_folder(self.folder_path)

    def _save_dump(self, path):
        if not path or self.dump_obj is None:
            self._show_error(self.tr.t("msg_template_required"))
            return
        tr = self.tr
        def work():
            return encrypt_tables(self.dump_obj, self.tables)
        def done(out):
            try:
                if os.path.exists(path):
                    shutil.copyfile(path, path + ".bak")
                write_dump(path, out)
                self.dirty = False
                self._update_status()
                self._toast(tr.t("msg_saved", name=os.path.basename(path)))
            except Exception as e:
                self._show_error(f"{tr.t('error_title')}: {e}")
        self._run_async(label=tr.t("encrypting", name=os.path.basename(path)), func=work, on_done=done)

    def _save_folder(self, folder):
        if not folder:
            self._show_error(self.tr.t("msg_template_required"))
            return
        tr = self.tr
        try:
            for name, rows in self.tables.items():
                with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2, ensure_ascii=False)
            self.dirty = False
            self._update_status()
            self._toast(tr.t("msg_exported", n=len(self.tables), folder=folder))
        except Exception as e:
            self._show_error(f"{tr.t('error_title')}: {e}")

    def save_as(self):
        if not self.tables:
            self._show_warning(self.tr.t("msg_no_file"))
            return
        self.commit_editor(silent=False)
        from tkinter import filedialog
        if self.dump_obj is not None:
            path = filedialog.asksaveasfilename(
                title=self.tr.t("save_as"), defaultextension=".json",
                filetypes=[("JSON", "*.json")])
            if path:
                self._save_dump(path)
        else:
            folder = filedialog.askdirectory(title=self.tr.t("save_as"))
            if folder:
                self._save_folder(folder)

    def export_folder_dialog(self):
        if not self.tables:
            self._show_warning(self.tr.t("msg_no_file"))
            return
        self.commit_editor(silent=False)
        from tkinter import filedialog
        folder = filedialog.askdirectory(title=self.tr.t("export_folder"))
        if not folder:
            return
        tr = self.tr
        try:
            n = 0
            for name, rows in self.tables.items():
                with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2, ensure_ascii=False)
                n += 1
            self._toast(tr.t("msg_exported", n=n, folder=folder))
        except Exception as e:
            self._show_error(f"{tr.t('error_title')}: {e}")


    # ---------------- data.unity3d integration (UABEADump) ----------------

    def _uabeadump_exe(self):
        """Locate the bundled UABEADump.exe helper."""
        candidates = [
            os.path.join(_bundled_data_dir(), "UABEADump", "UABEADump.exe"),
            os.path.join(TOOL_DIR, "UABEADump", "UABEADump.exe"),
            os.path.join(TOOL_DIR, "tools", "UABEADump", "UABEADump.exe"),
            os.path.join(TOOL_DIR, "UABEADump.exe"),
            os.path.join(os.path.dirname(sys.executable), "UABEADump", "UABEADump.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def _steam_install_path(self):
        """Steam install directory from the registry, or None."""
        try:
            import winreg
        except Exception:
            return None
        for root, key, val in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(root, key) as k:
                    v, _ = winreg.QueryValueEx(k, val)
                    if v and os.path.isdir(v):
                        return v
            except Exception:
                continue
        return None

    def _parse_libraryfolders(self, vdf_path):
        """Library folder paths from a Steam libraryfolders.vdf file."""
        if not vdf_path or not os.path.isfile(vdf_path):
            return []
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return []
        import re
        out = []
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if p and os.path.isdir(p):
                out.append(p)
        for m in re.finditer(r'"\d+"\s+"((?:[^"\\]|\\.)*)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if p and os.path.isdir(p):
                out.append(p)
        return out

    def _steam_library_common_dirs(self):
        """Candidate steamapps/common directories (registry + VDF + known paths)."""
        steam = self._steam_install_path()
        roots = []
        if steam:
            roots.append(os.path.join(steam, "steamapps", "common"))
            for lf in (os.path.join(steam, "steamapps", "libraryfolders.vdf"),
                       os.path.join(steam, "config", "libraryfolders.vdf")):
                for lib in self._parse_libraryfolders(lf):
                    roots.append(os.path.join(lib, "steamapps", "common"))
        roots += [r"C:\Program Files (x86)\Steam\steamapps\common",
                  r"C:\Program Files\Steam\steamapps\common",
                  r"<游戏安装目录>",
                  r"<游戏库目录>"]
        seen, out = set(), []
        for r in roots:
            r = os.path.normpath(r)
            if r and r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def _game_data_dir(self):
        """BrokenArrow_Data directory (needed for IL2CPP MonoBehaviour reflection)."""
        s = load_settings()
        d = s.get("game_data_dir")
        if d and os.path.isdir(d):
            return d
        import glob
        for root in self._steam_library_common_dirs():
            try:
                entries = glob.glob(os.path.join(root, "*"))
            except Exception:
                continue
            for g in entries:
                if not os.path.basename(g).lower().startswith(("broken", "arrow")):
                    continue
                data = os.path.join(g, "BrokenArrow_Data")
                if os.path.isdir(data):
                    s["game_data_dir"] = data
                    save_settings(s)
                    return data
        return None

    def _run_uabeadump(self, args, unity3d_path=None):
        """Run UABEADump.exe with args. Returns (ok, message)."""
        exe = self._uabeadump_exe()
        if not exe:
            return False, self.tr.t("msg_need_helper")
        # The selected data.unity3d always lives inside BrokenArrow_Data, so its
        # parent directory is the most reliable way to locate the game data dir
        # (works even when Steam is installed on a non-standard drive/path).
        game = None
        if unity3d_path:
            d = os.path.dirname(os.path.abspath(unity3d_path))
            if os.path.isdir(d):
                game = d
                s = load_settings()
                if s.get("game_data_dir") != d:
                    s["game_data_dir"] = d
                    save_settings(s)
        if not game:
            game = self._game_data_dir()
        if not game:
            return False, self.tr.t("msg_need_game_dir")
        import subprocess
        cmd = [exe] + args + ["--game", game]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if p.returncode != 0:
                return False, (p.stderr or p.stdout or "exit %d" % p.returncode).strip()
            return True, (p.stdout or "").strip()
        except Exception as e:
            return False, str(e)

    def export_from_unity3d(self):
        """Export the DataBaseCompiled asset from data.unity3d to a JSON dump."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self.tr.t("select_unity3d"),
            filetypes=[("Unity bundle", "*.unity3d"), ("All files", "*.*")])
        if not path:
            return
        out = filedialog.asksaveasfilename(
            title=self.tr.t("export_unity3d"), defaultextension=".json",
            filetypes=[("JSON", "*.json")])
        if not out:
            return
        tr = self.tr
        def work():
            return self._run_uabeadump(["extract", path, "-o", out], path)
        def done(res):
            ok, msg = res
            if ok:
                self._toast(tr.t("msg_exported_unity3d", path=out))
            else:
                self._show_error(f"{tr.t('error_title')}: {tr.t('msg_helper_failed', error=msg)}")
        self._run_async(label=tr.t("msg_extracting"), func=work, on_done=done)

    def open_from_unity3d(self):
        """Extract DataBaseCompiled from data.unity3d and open it in the editor."""
        if not self._confirm_discard():
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self.tr.t("select_unity3d"),
            filetypes=[("Unity bundle", "*.unity3d"), ("All files", "*.*")])
        if not path:
            return
        import tempfile
        import time
        tmp = os.path.join(tempfile.gettempdir(), "ba_db_from_unity3d_%d.json" % int(time.time()))
        tr = self.tr
        def work():
            return self._run_uabeadump(["extract", path, "-o", tmp], path)
        def done(res):
            ok, msg = res
            if not ok:
                self._show_error(f"{tr.t('error_title')}: {tr.t('msg_helper_failed', error=msg)}")
                return
            if not os.path.isfile(tmp):
                self._show_error(f"{tr.t('error_title')}: {tr.t('msg_helper_failed', error='no output file')}")
                return
            self.open_dump(tmp, unity3d_path=path)
        self._run_async(label=tr.t("msg_extracting"), func=work, on_done=done)

    def _write_to_unity3d(self, path):
        """Encrypt current tables and write them into data.unity3d (in-place)."""
        if not self.tables:
            self._show_warning(self.tr.t("msg_no_file"))
            return
        self.commit_editor(silent=False)
        if self.dump_obj is None:
            self._show_error(self.tr.t("msg_template_required"))
            return
        import tempfile
        import time
        tmp = os.path.join(tempfile.gettempdir(), "ba_db_import_%d.json" % int(time.time()))
        tr = self.tr
        def work():
            out = encrypt_tables(self.dump_obj, self.tables)
            write_dump(tmp, out)
            cmd = ["import", path, tmp, "-o", path]
            if load_settings().get("compress_on_save"):
                cmd.append("--compressed")
            return self._run_uabeadump(cmd, path)
        def done(res):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            ok, msg = res
            if ok:
                self.dirty = False
                self._update_status()
                self._toast(tr.t("msg_unity3d_imported", name=os.path.basename(path)))
            else:
                self._show_error(f"{tr.t('error_title')}: {tr.t('msg_helper_failed', error=msg)}")
        self._run_async(label=tr.t("msg_importing"), func=work, on_done=done)

    def import_into_unity3d(self):
        """Import the current database into data.unity3d (menu entry)."""
        if self.unity3d_path:
            self._write_to_unity3d(self.unity3d_path)
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title=self.tr.t("select_unity3d"),
            filetypes=[("Unity bundle", "*.unity3d"), ("All files", "*.*")])
        if not path:
            return
        self._write_to_unity3d(path)


    # ---------------- relations (关联视图) ----------------

    def _ensure_indexes(self):
        """Lazy indexes: id->row, unit->link rows, turret->weapon rows,
        modification->option rows. Rebuilt whenever data changes."""
        if self._rel_ver == self._data_version:
            return
        self._id_index = {}
        self._by_unit = {}
        self._by_turret = {}
        self._by_mod = {}
        for tname, rows in self.tables.items():
            idm = {}
            for i, r in enumerate(rows):
                if isinstance(r, dict) and isinstance(r.get("Id"), int):
                    idm.setdefault(r["Id"], i)
            self._id_index[tname] = idm
            if tname in ("UnitAbilities", "TurretUnits", "SquadWeapons", "SquadMembers",
                         "WeaponAmmunitions", "UnitArmors", "UnitPropulsions",
                         "SensorUnits", "Modifications"):
                m = {}
                for i, r in enumerate(rows):
                    if isinstance(r, dict) and isinstance(r.get("UnitId"), int):
                        m.setdefault(r["UnitId"], []).append(i)
                self._by_unit[tname] = m
            elif tname == "TurretWeapons":
                m = {}
                for i, r in enumerate(rows):
                    if isinstance(r, dict) and isinstance(r.get("TurretId"), int):
                        m.setdefault(r["TurretId"], []).append(i)
                self._by_turret[tname] = m
            elif tname == "Options":
                m = {}
                for i, r in enumerate(rows):
                    if isinstance(r, dict) and isinstance(r.get("ModificationId"), int):
                        m.setdefault(r["ModificationId"], []).append(i)
                self._by_mod[tname] = m
        self._rel_ver = self._data_version

    def _field_label(self, table, field):
        """Localized (Chinese) label for a raw-data field name (e.g.
        DisplayInArmory -> 是否在军械库显示); falls back to the raw name."""
        try:
            name, _desc = field_info(table, field, self.tr.lang)
            if name:
                return name
        except Exception:
            pass
        return field

    def _display_name(self, table, row):
        """Best human-readable, localized display name for a row.

        Localization keys (Options.UIName 'Custom_Option_*',
        Modifications.UIName 'Custom_Slot_*', Specializations.UIName
        'ui_spec_*') are resolved to the current language; plain English
        designations (units / weapons / ammo) are left untouched.

        SpecializationAvailabilities rows carry no own name — a row means
        "unit <UnitId> is offered in <SpecializationId>", so they are named
        after that unit (e.g. Force Recon).
        """
        lang = self.tr.lang
        if table == "SpecializationAvailabilities":
            self._ensure_indexes()
            uid2 = row.get("UnitId")
            if isinstance(uid2, int) and uid2 > 0:
                uidx = self._id_index.get("Units", {}).get(uid2)
                if uidx is not None:
                    return self._display_name("Units", self.tables["Units"][uidx])
                return f"Unit {uid2}"
        un = row.get("UIName")
        if isinstance(un, str) and un.strip():
            loc = localize_text(un, lang)
            if loc != un:
                return loc
            return COUNTRY_NAMES.get(un, {}).get(lang, un)
        for k in ("Name", "HUDName", "ModelFileName"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return localize_text(v, lang)
        return "?"

    def _row_name(self, table, rid):
        idx = self._id_index.get(table, {}).get(rid)
        if idx is None:
            return f"Id={rid}"
        row = self.tables[table][idx]
        name = self._display_name(table, row)
        if not name:
            for k in ("Name", "HUDName", "UIName", "ModelFileName"):
                v = row.get(k)
                if isinstance(v, str) and v:
                    return v
            return f"Id={rid}"
        return name

    def _row_field(self, table, rid, field):
        """Return one field value of a referenced row, or None."""
        idx = self._id_index.get(table, {}).get(rid)
        if idx is None:
            return None
        row = self.tables[table][idx]
        return row.get(field) if isinstance(row, dict) else None

    def _fk_name_for(self, field, target, rid):
        """Short human name for an FK value.

        SpecializationAvailabilities rows mean "unit X is offered in this
        specialization", so TransportAvailabilities.SpecializationAvailabilityId
        (and anything referencing an availability) resolves to that UNIT:
        104 -> 'Force Recon (61)'. Everything else keeps _row_name."""
        if field == "SpecializationAvailabilityId" and target == "SpecializationAvailabilities":
            self._ensure_indexes()
            uid2 = self._row_field(target, rid, "UnitId")
            if isinstance(uid2, int) and uid2 > 0:
                uidx = self._id_index.get("Units", {}).get(uid2)
                if uidx is not None:
                    return "%s (%d)" % (self._display_name("Units",
                                                           self.tables["Units"][uidx]), uid2)
        return self._row_name(target, rid)

    # ---------------- unit stat info (detail banner) ----------------

    def _unit_armor(self, uid):
        for li in self._by_unit.get("UnitArmors", {}).get(uid, []):
            row = self.tables["UnitArmors"][li]
            aid = row.get("ArmorId")
            idx = self._id_index.get("Armors", {}).get(aid)
            if idx is not None and self.tables["Armors"][idx].get("IsDefault"):
                return self.tables["Armors"][idx]
        return None

    def _unit_mobility(self, uid):
        for li in self._by_unit.get("UnitPropulsions", {}).get(uid, []):
            row = self.tables["UnitPropulsions"][li]
            mid = row.get("MobilityId")
            idx = self._id_index.get("Mobility", {}).get(mid)
            if idx is not None:
                return self.tables["Mobility"][idx]
        return None

    def _unit_flypreset(self, uid):
        mob = self._unit_mobility(uid)
        fpid = mob.get("FlyPresetId") if mob else None
        idx = self._id_index.get("FlyPresets", {}).get(fpid)
        return self.tables["FlyPresets"][idx] if idx is not None else None

    def _unit_sensor(self, uid):
        for li in self._by_unit.get("SensorUnits", {}).get(uid, []):
            row = self.tables["SensorUnits"][li]
            sid = row.get("SensorId")
            idx = self._id_index.get("Sensors", {}).get(sid)
            if idx is not None and self.tables["Sensors"][idx].get("IsDefault"):
                return self.tables["Sensors"][idx]
        return None

    def _unit_supply_sum(self, uid):
        """Total resupply time (s) of all ammo bound to this unit."""
        total = 0.0
        for wi in self._by_unit.get("WeaponAmmunitions", {}).get(uid, []):
            wa = self.tables["WeaponAmmunitions"][wi]
            aid = wa.get("AmmunitionId")
            idx = self._id_index.get("Ammunitions", {}).get(aid)
            if idx is None:
                continue
            v = self.tables["Ammunitions"][idx].get("ResupplyTime")
            if isinstance(v, (int, float)):
                total += v
        return total

    def _unit_squad_count(self, uid):
        return len(self._by_unit.get("SquadMembers", {}).get(uid, []))

    def _unit_ability_rows(self, uid):
        out = []
        for li in self._by_unit.get("UnitAbilities", {}).get(uid, []):
            row = self.tables["UnitAbilities"][li]
            aid = row.get("AbilityId")
            idx = self._id_index.get("Abilities", {}).get(aid)
            if idx is not None:
                out.append(self.tables["Abilities"][idx])
        return out

    @staticmethod
    def _num(v, nd=1):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return str(v)
        if v == int(v):
            return str(int(v))
        return ("%." + str(nd) + "f") % v

    def _unit_ability_labels(self, uid, mob):
        """Curated boolean capability labels from abilities + mobility."""
        L = self.tr.lang
        seen = []
        def push(key):
            lab = INFO_LABEL[key].get(L, INFO_LABEL[key]["en"])
            if lab not in seen:
                seen.append(lab)
        for ab in self._unit_ability_rows(uid):
            if ab.get("IsInfantrySprint"):
                push("sprint")
            if ab.get("IsSmoke"):
                push("smoke")
            if ab.get("IsAPS"):
                push("aps")
            if ab.get("IsDecoy"):
                push("decoy")
            if ab.get("IsRadar"):
                push("radar")
            if ab.get("IsLaserDesignator"):
                push("laser")
            if ab.get("CanHeal"):
                push("heal")
            if ab.get("CanRepair"):
                push("repair")
            if ab.get("CanResupply") or ab.get("IsResupplyAbility"):
                push("resupply")
            if ab.get("IsSneakAbility"):
                push("sneak")
            if ab.get("IsArtilleryAutoFire"):
                push("autofire")
            if ab.get("CanSelfSupply"):
                push("selfsupply")
        if mob:
            if mob.get("IsAirDroppable"):
                push("airdrop")
            if mob.get("IsAmphibious"):
                push("amphibious")
            if mob.get("IsAfterburner"):
                push("afterburn")
        return seen

    def _unit_info_lines(self, uid):
        """Human-readable stat lines for the detail banner, plus per-line
        hover tooltips. Returns a list of (text, tooltip_or_None)."""
        self._ensure_indexes()
        L = self.tr.lang
        def t(key):
            return INFO_LABEL[key].get(L, INFO_LABEL[key]["en"])
        uidx = self._id_index.get("Units", {}).get(uid)
        if uidx is None:
            return []
        u = self.tables["Units"][uidx]
        armor = self._unit_armor(uid)
        mob = self._unit_mobility(uid)
        sensor = self._unit_sensor(uid)
        fly = self._unit_flypreset(uid)
        squad = self._unit_squad_count(uid)
        stealth = u.get("Stealth")
        cat = u.get("CategoryType")
        lines = []
        def add(text, tip=None):
            lines.append((text, tip))
        def fv(v, nd=1):
            return self._num(v, nd)

        # vision (×2, metres)
        vg = vl = vh = 0
        if sensor:
            vg = sensor.get("OpticsGround") or 0
            vl = sensor.get("OpticsLowAltitude") or 0
            vh = sensor.get("OpticsHighAltitude") or 0
        vision_tip = ("%s: %s %s\n%s: %s %s\n%s: %s %s" % (
            t("vision_g"), fv(vg * 2), t("m"),
            t("vision_l"), fv(vl * 2), t("m"),
            t("vision_h"), fv(vh * 2), t("m")))

        caps = self._unit_ability_labels(uid, mob)
        caps_txt = " / ".join(caps) if caps else t("none")

        is_heli = cat == 5
        # fixed-wing aircraft: the air category, or any flying unit that is not
        # a helicopter (recon-branch fixed-wing UAVs have CategoryType 0)
        is_plane = cat == 6 or (fly is not None and cat != 5)
        is_inf = squad > 0 or bool(self._by_unit.get("SquadWeapons", {}).get(uid))

        if is_inf:
            if armor:
                add("%s: %s" % (t("armor"), fv(armor.get("ArmorValue"))))
                add("%s: %s" % (t("health"), fv(armor.get("MaxHealthPoints"))))
            add("%s: %s" % (t("squad"), squad))
            add("%s: %s %s" % (t("vision"), fv(vg * 2), t("m")), vision_tip)
            if stealth is not None and stealth:
                add("%s: %s" % (t("stealth"), fv(round(1.0 / stealth, 2), 2)))
            if mob:
                add("%s: %s" % (t("speed"), fv(mob.get("MaxSpeedRoad"))))
            add("%s: %s %s" % (t("weight"), fv(125 * squad), t("kg")))
            add("%s: %s" % (t("ability"), caps_txt))
        elif is_heli:
            if armor:
                add("%s: %s" % (t("armor"), fv(armor.get("ArmorValue"))))
                add("%s: %s" % (t("health"), fv(armor.get("MaxHealthPoints"))))
            if mob:
                add("%s: %s" % (t("cargo"), fv(mob.get("HeavyLiftWeight"))))
            add("%s: %s" % (t("seats"), fv(u.get("InfantrySlots"))))
            add("%s: %s %s" % (t("vision"), fv(vg * 2), t("m")), vision_tip)
            if stealth is not None and stealth:
                add("%s: %s" % (t("stealth"), fv(round(1.0 / stealth, 2), 2)))
            if mob:
                add("%s: %s" % (t("speed"), fv(mob.get("MaxSpeedRoad"))))
                add("%s: %s" % (t("agility"), fv(mob.get("Agility"))))
            add("%s: %s" % (t("ability"), caps_txt))
        elif is_plane:
            if armor:
                add("%s: %s" % (t("armor"), fv(armor.get("ArmorValue"))))
                add("%s: %s" % (t("health"), fv(armor.get("MaxHealthPoints"))))
            if mob:
                add("%s: %s" % (t("cargo"), fv(mob.get("HeavyLiftWeight"))))
            add("%s: %s" % (t("seats"), fv(u.get("InfantrySlots"))))
            add("%s: %s %s" % (t("vision"), fv(vg * 2), t("m")), vision_tip)
            if stealth is not None and stealth:
                add("%s: %s" % (t("stealth"), fv(round(1.0 / stealth, 2), 2)))
            if fly:
                fwd = (fly.get("MaxSpeed") or 0) * 3.6
                add("%s: %s %s" % (t("speed"), fv(int(round(fwd / 10.0)) * 10), t("kmh")))
                ms = fly.get("MaxSpeed")
                myaw = fly.get("MaxSpeedYaw")
                if isinstance(ms, (int, float)) and isinstance(myaw, (int, float)) and myaw:
                    th = math.radians(myaw)
                    radius = int(2.0 * ms * (math.tan(th) + 1.0) / math.tan(th) / 5.0) * 5
                    turn_tip = ("2 × MaxSpeed × (tan(MaxSpeedYaw×π/180)+1) ÷ tan(MaxSpeedYaw×π/180) = 2 × %s × (tan(%s°)+1) ÷ tan(%s°)"
                                % (fv(ms), fv(myaw), fv(myaw)))
                    add("%s: %s %s" % (t("turn"), fv(radius), t("m")), turn_tip)
                else:
                    add("%s: %s" % (t("turn"), fv(ms)))
            if mob:
                add("%s: %s" % (t("fuel"), fv(mob.get("LoiteringTime"))))
            add("%s: %s" % (t("ability"), caps_txt))
        else:  # vehicle (and anything else)
            if armor:
                kin = "%s %s/%s  %s %s/%s  %s %s/%s  %s %s/%s" % (
                    t("front"), fv(armor.get("KinArmorFront")), fv(armor.get("HeatArmorFront")),
                    t("top"), fv(armor.get("KinArmorTop")), fv(armor.get("HeatArmorTop")),
                    t("side"), fv(armor.get("KinArmorSides")), fv(armor.get("HeatArmorSides")),
                    t("rear"), fv(armor.get("KinArmorRear")), fv(armor.get("HeatArmorRear")))
                add("%s/%s: %s" % (t("kin"), t("heat"), kin))
                add("%s: %s" % (t("health"), fv(armor.get("MaxHealthPoints"))))
            if mob:
                add("%s: %s" % (t("cargo"), fv(mob.get("HeavyLiftWeight"))))
            add("%s: %s" % (t("seats"), fv(u.get("InfantrySlots"))))
            add("%s: %s %s" % (t("vision"), fv(vg * 2), t("m")), vision_tip)
            if stealth is not None and stealth:
                add("%s: %s" % (t("stealth"), fv(round(1.0 / stealth, 2), 2)))
            if mob:
                offroad = mob.get("MaxCrossCountrySpeed")
                add("%s: %s" % (t("speed"), fv(mob.get("MaxSpeedRoad"))),
                    ("%s: %s" % (t("offroad"), fv(offroad))) if offroad is not None else None)
                add("%s: %s" % (t("reverse"), fv(mob.get("MaxSpeedReverse"))))
            add("%s: %s" % (t("weight"), fv(u.get("Weight"))))
            add("%s: %s" % (t("ability"), caps_txt))
        return lines

    def _rel_target(self, table, rid):
        idx = self._id_index.get(table, {}).get(rid)
        if idx is None:
            return None
        return (table, idx)

    def _build_relations_into(self, tree, rel_map, table, row_idx, owner=None):
        """Fill a relations tree for (table, row_idx). owner is the DetailWindow
        that owns the tree, so right-click add actions act on the right window."""
        self._snapshot_rel_open(tree, rel_map)
        tree.delete(*tree.get_children())
        rel_map.clear()
        if table is None or table not in self.tables:
            tree.insert("", "end", text=self.tr.t("editor_no_row"))
            return
        rows = self.tables[table]
        if not (0 <= row_idx < len(rows)):
            tree.insert("", "end", text=self.tr.t("editor_no_row"))
            return
        row = rows[row_idx]
        if not isinstance(row, dict):
            tree.insert("", "end", text=self.tr.t("editor_no_row"))
            return
        self._ensure_indexes()
        if table == "Units":
            self._build_unit_relations(tree, rel_map, row, owner)
        else:
            self._build_generic_relations(tree, rel_map, table, row)
        self._restore_rel_open(tree, rel_map)
        self._update_fold_marks(tree, rel_map)

    @staticmethod
    def _rel_state_key(payload, text):
        """Stable identity for a relation-tree node across rebuilds."""
        if payload is not None:
            kind = payload.get("kind")
            if kind == "group":
                return ("group", payload.get("group"))
            if kind == "unit":
                return ("unit",)
            key = payload.get("key")
            if key is not None:
                return key
            edit = payload.get("edit")
            nav = payload.get("nav")
            return ("item", edit if edit is not None else nav)
        return ("text", text)

    def _snapshot_rel_open(self, tree, rel_map):
        """Record expanded nodes into the global self._rel_fold, so fold state
        survives navigation and rebuilds."""
        def walk(iid):
            for c in tree.get_children(iid):
                key = self._rel_state_key(rel_map.get(c), tree.item(c, "text"))
                if tree.get_children(c):
                    self._rel_fold[key] = bool(tree.item(c, "open"))
                walk(c)
        walk("")

    def _restore_rel_open(self, tree, rel_map, state=None):
        """Re-apply the globally remembered expanded/collapsed state."""
        def walk(iid):
            for c in tree.get_children(iid):
                key = self._rel_state_key(rel_map.get(c), tree.item(c, "text"))
                if key in self._rel_fold:
                    tree.item(c, open=self._rel_fold[key])
                walk(c)
        walk("")

    def _rel_icon(self, text):
        """Pull a leading colour emoji out of text into a PhotoImage.
        Returns (image, text-without-the-emoji). Falls back to keeping the
        plain character when no icon file is available."""
        for char, code in EMOJI_CHAR_TO_CODE.items():
            if text.startswith(char):
                img = icon_image(self.root, code, 24)
                if img is not None:
                    self._icon_refs[code] = img
                    return img, text[len(char):].lstrip()
                break
        return None, text

    def _rel_group(self, tree, parent, title):
        img, title = self._rel_icon(title)
        g = tree.insert(parent, "end", text=title,
                        image=img if img is not None else "")
        # Fold bars are collapsed on first build; _restore_rel_open re-applies
        # the user's previous expanded/collapsed state after a refresh.
        tree.item(g, open=False)
        return g

    def _rel_item(self, tree, rel_map, parent, text, nav, edit=None, game_icon=None):
        """nav: where double-click goes. edit: row shown in the mini editor
        and removed by Delete (None = not editable/deletable). Items start
        collapsed; _restore_rel_open re-opens any the user expanded.

        game_icon: an icon asset name (HUDIcon / OptionPicture) that, when
        resolvable, replaces the leading emoji with the real game icon."""
        img = None
        if game_icon:
            img = game_icon_image(self.root, game_icon, 56, 56)
            if img is not None:
                self._icon_refs["g:" + str(game_icon)] = img
                _, text = self._rel_icon(text)  # strip the leading emoji
        if img is None:
            img, text = self._rel_icon(text)
        iid = tree.insert(parent, "end", text=text,
                          image=img if img is not None else "")
        tree.item(iid, open=False)
        rel_map[iid] = {"nav": nav, "edit": edit if edit is not None else nav,
                        "text": text}
        return iid

    def _update_fold_mark(self, tree, rel_map, iid):
        """Draw the big "▼ / ▶" marker for any node that has children."""
        if not tree.get_children(iid):
            return
        payload = rel_map.get(iid)
        base = payload.get("text") if payload else None
        if base is None:
            base = tree.item(iid, "text")
            if base.startswith(FOLD_OPEN):
                base = base[len(FOLD_OPEN):]
            elif base.startswith(FOLD_CLOSED):
                base = base[len(FOLD_CLOSED):]
        mark = FOLD_OPEN if tree.item(iid, "open") else FOLD_CLOSED
        tree.item(iid, text=mark + base)

    def _update_fold_marks(self, tree, rel_map):
        def walk(iid):
            self._update_fold_mark(tree, rel_map, iid)
            for c in tree.get_children(iid):
                walk(c)
        for c in tree.get_children(""):
            walk(c)

    def _sort_by_order_id(self, table, indices):
        """Sort row indices by (Order, Id) so a fold bar lists items in the
        game's intended order. Missing/invalid values sort first."""
        rows = self.tables.get(table)
        if not rows:
            return list(indices)

        def key(i):
            r = rows[i] if 0 <= i < len(rows) else None
            if not isinstance(r, dict):
                return (0, 0)
            order = r.get("Order")
            oid = r.get("Id")
            return (order if isinstance(order, (int, float)) else 0,
                    oid if isinstance(oid, int) else 0)
        return sorted(indices, key=key)

    def _table_has_order(self, table):
        """True if any row of this table carries an 'Order' field."""
        rows = self.tables.get(table, [])
        return any(isinstance(r, dict) and "Order" in r for r in rows)

    def _build_unit_relations(self, tree, rel_map, row, owner=None):
        """Build the relation tree for one Units row.

        Layout: a unit header fold bar, then one collapsible group per relation
        kind (abilities / turrets / infantry weapons & members / ammo / armors /
        mobility / sensors / modifications). Nested items are added via
        _rel_item, which assigns each a colour emoji icon (see _rel_icon).
        """
        tr = self.tr
        self._ensure_indexes()
        uid = row.get("Id") if isinstance(row.get("Id"), int) else None

        header = f"{tr.t('unit_header')} " + str(uid if uid is not None else "?")
        name = row.get("Name") or row.get("HUDName") or ""
        if name:
            header += f" — {name}"
        if isinstance(row.get("CountryId"), int) and row["CountryId"] > 0:
            header += f"  [{self._row_name('Countries', row['CountryId'])}]"
        if isinstance(row.get("Cost"), (int, float)):
            header += f"  Cost={row['Cost']}"
        h = tree.insert("", "end", text=header)
        # Unit header is a fold bar too: collapsed on first build.
        tree.item(h, open=False)
        rel_map[h] = {"kind": "unit", "nav": None, "edit": None, "id": uid,
                      "text": header}

        # 技能
        ab = self._by_unit.get("UnitAbilities", {}).get(uid, [])
        g = self._rel_group(tree, "", tr.t("rel_group_abilities"))
        rel_map[g] = {"kind": "group", "group": "abilities", "nav": None, "edit": None}
        if ab:
            for li in ab:
                link = self.tables["UnitAbilities"][li]
                aid = link.get("AbilityId")
                if isinstance(aid, int) and aid > 0:
                    text = f"⚡ {self._row_name('Abilities', aid)}  [Abilities.Id={aid}]"
                    self._rel_item(tree, rel_map, g, text, self._rel_target("Abilities", aid),
                                    ("UnitAbilities", li))
                else:
                    self._rel_item(tree, rel_map, g, f"[UnitAbilities.Id={link.get('Id')}] AbilityId=0 ({tr.t('rel_not_set')})",
                                   ("UnitAbilities", li))

        # 炮塔 (TurretUnits → Turrets → TurretWeapons)
        tu = self._sort_by_order_id("TurretUnits", self._by_unit.get("TurretUnits", {}).get(uid, []))
        g = self._rel_group(tree, "", tr.t("rel_group_turrets"))
        rel_map[g] = {"kind": "group", "group": "turrets", "nav": None, "edit": None}
        for li in tu:
            link = self.tables["TurretUnits"][li]
            tid = link.get("TurretId")
            if isinstance(tid, int) and tid > 0:
                turret_target = self._rel_target("Turrets", tid)
                tparent = self._rel_item(tree, rel_map, g,
                                         f"🗼 {tr.t('rel_turret_mount')}: {self._row_name('Turrets', tid)}  [TurretUnits.Id={link.get('Id')}]",
                                         turret_target,
                                         ("TurretUnits", li))
                tw_parent = tparent
                if turret_target is not None:
                    # The middle level of the chain TurretUnits → Turrets →
                    # TurretWeapons: make the Turret row editable right here,
                    # without a double-click jump. Deletion stays on the mount
                    # row above; a Turret is shared by many units and must not
                    # be removable from this tree.
                    tnode = self._rel_item(tree, rel_map, tparent,
                                           f"🗼 {tr.t('rel_turret_data')}: {self._row_name('Turrets', tid)}  [Turrets.Id={tid}]",
                                           turret_target,
                                           turret_target)
                    rel_map[tnode]["delete"] = None
                    # Duplication stays enabled: it copies this Turret to a
                    # brand-new Id and never touches units that share it.
                    tw_parent = tnode
                for wi in self._sort_by_order_id("TurretWeapons", self._by_turret.get("TurretWeapons", {}).get(tid, [])):
                    tw = self.tables["TurretWeapons"][wi]
                    wid = tw.get("WeaponId")
                    if isinstance(wid, int) and wid > 0:
                        wnode = self._rel_item(tree, rel_map, tw_parent,
                                               f"🔫 {tr.t('rel_weapon')}: {self._row_name('Weapons', wid)}  [Weapons.Id={wid}]",
                                               self._rel_target("Weapons", wid),
                                               ("TurretWeapons", wi),
                                               game_icon=self._row_field("Weapons", wid, "HUDIcon"))
                        self._rel_ammo_under(tree, rel_map, wnode, uid, wid)
                    else:
                        self._rel_item(tree, rel_map, tw_parent,
                                       f"[TurretWeapons.Id={tw.get('Id')}] WeaponId=0 ({tr.t('rel_not_set')})",
                                       ("TurretWeapons", wi))
            else:
                self._rel_item(tree, rel_map, g,
                               f"[TurretUnits.Id={link.get('Id')}] TurretId=0 ({tr.t('rel_not_set')})",
                               ("TurretUnits", li))

        # 步兵武器 (SquadWeapons)
        sw = self._sort_by_order_id("SquadWeapons", self._by_unit.get("SquadWeapons", {}).get(uid, []))
        g = self._rel_group(tree, "", tr.t("rel_group_squad_weapons"))
        rel_map[g] = {"kind": "group", "group": "squad_weapons", "nav": None, "edit": None}
        if sw:
            for li in sw:
                link = self.tables["SquadWeapons"][li]
                wid = link.get("WeaponId")
                if isinstance(wid, int) and wid > 0:
                    self._rel_item(tree, rel_map, g, f"🔫 {self._row_name('Weapons', wid)}  [Weapons.Id={wid}]",
                                   self._rel_target("Weapons", wid), ("SquadWeapons", li),
                                   game_icon=self._row_field("Weapons", wid, "HUDIcon"))
                else:
                    self._rel_item(tree, rel_map, g, f"[SquadWeapons.Id={link.get('Id')}] WeaponId=0 ({tr.t('rel_not_set')})",
                                   ("SquadWeapons", li))

        # 步兵成员 (SquadMembers) — 主/副武器嵌套，武器下挂弹药
        sm = self._by_unit.get("SquadMembers", {}).get(uid, [])
        g = self._rel_group(tree, "", tr.t("rel_group_squad_members"))
        rel_map[g] = {"kind": "group", "group": "squad_members", "nav": None, "edit": None}
        if sm:
            for li in sm:
                link = self.tables["SquadMembers"][li]
                mtext = f"👤 {tr.t('rel_member')}: {link.get('ModelFileName') or link.get('Id')}"
                mparent = self._rel_item(tree, rel_map, g, mtext,
                                         ("SquadMembers", li), ("SquadMembers", li))
                self._rel_member_weapon(tree, rel_map, mparent, uid,
                                        link.get("PrimaryWeaponId"), tr.t("rel_primary_weapon"),
                                        li, "PrimaryWeaponId")
                self._rel_member_weapon(tree, rel_map, mparent, uid,
                                        link.get("SpecialWeaponId"), tr.t("rel_secondary_weapon"),
                                        li, "SpecialWeaponId")

        # 弹药
        wa = self._sort_by_order_id("WeaponAmmunitions", self._by_unit.get("WeaponAmmunitions", {}).get(uid, []))
        g = self._rel_group(tree, "", tr.t("rel_group_ammo"))
        rel_map[g] = {"kind": "group", "group": "ammo", "nav": None, "edit": None}
        for li in wa:
            link = self.tables["WeaponAmmunitions"][li]
            wid = link.get("WeaponId")
            aid = link.get("AmmunitionId")
            text = f"💣 {self._row_name('Weapons', wid) if isinstance(wid, int) else '?'} ← "
            text += f"{self._row_name('Ammunitions', aid) if isinstance(aid, int) else '?'}"
            if isinstance(link.get("Quantity"), (int, float)):
                text += f" ×{link['Quantity']}"
            if isinstance(aid, int) and aid > 0:
                text += f"  [Ammunitions.Id={aid}]"
                self._rel_item(tree, rel_map, g, text, self._rel_target("Ammunitions", aid),
                               ("WeaponAmmunitions", li),
                               game_icon=self._row_field("Ammunitions", aid, "HUDIcon"))
            else:
                self._rel_item(tree, rel_map, g, text, ("WeaponAmmunitions", li))

        # 装甲
        ua = self._by_unit.get("UnitArmors", {}).get(uid, [])
        g = self._rel_group(tree, "", tr.t("rel_group_armors"))
        rel_map[g] = {"kind": "group", "group": "armors", "nav": None, "edit": None}
        if ua:
            for li in ua:
                link = self.tables["UnitArmors"][li]
                aid = link.get("ArmorId")
                if isinstance(aid, int) and aid > 0:
                    text = "🛡 " + self._row_name("Armors", aid)
                    aidx = self._id_index.get("Armors", {}).get(aid)
                    if aidx is not None and self.tables["Armors"][aidx].get("IsDefault"):
                        text += f"  ★{tr.t('rel_default_mark')}"
                    text += f"  [Armors.Id={aid}]"
                    self._rel_item(tree, rel_map, g, text, self._rel_target("Armors", aid),
                                    ("UnitArmors", li))
                else:
                    self._rel_item(tree, rel_map, g, f"[UnitArmors.Id={link.get('Id')}] ArmorId=0 ({tr.t('rel_not_set')})",
                                   ("UnitArmors", li))

        # 机动
        up = self._by_unit.get("UnitPropulsions", {}).get(uid, [])
        g = self._rel_group(tree, "", tr.t("rel_group_mobility"))
        rel_map[g] = {"kind": "group", "group": "mobility", "nav": None, "edit": None}
        if up:
            for li in up:
                link = self.tables["UnitPropulsions"][li]
                mid = link.get("MobilityId")
                if isinstance(mid, int) and mid > 0:
                    parent = self._rel_item(tree, rel_map, g,
                                            f"🏎 {self._row_name('Mobility', mid)}  [Mobility.Id={mid}]",
                                            self._rel_target("Mobility", mid),
                                            ("UnitPropulsions", li))
                    midx = self._id_index.get("Mobility", {}).get(mid)
                    if midx is not None:
                        fpid = self.tables["Mobility"][midx].get("FlyPresetId")
                        if isinstance(fpid, int) and fpid > 0:
                            self._rel_item(tree, rel_map, parent,
                                           f"✈ {tr.t('rel_flight')}: {self._row_name('FlyPresets', fpid)}  [FlyPresets.Id={fpid}]",
                                           self._rel_target("FlyPresets", fpid), None)
                else:
                    self._rel_item(tree, rel_map, g, f"[UnitPropulsions.Id={link.get('Id')}] MobilityId=0 ({tr.t('rel_not_set')})",
                                   ("UnitPropulsions", li))

        # 传感器
        su = self._by_unit.get("SensorUnits", {}).get(uid, [])
        g = self._rel_group(tree, "", tr.t("rel_group_sensors"))
        rel_map[g] = {"kind": "group", "group": "sensors", "nav": None, "edit": None}
        if su:
            for li in su:
                link = self.tables["SensorUnits"][li]
                sid = link.get("SensorId")
                if isinstance(sid, int) and sid > 0:
                    text = f"📡 {self._row_name('Sensors', sid)}  [Sensors.Id={sid}]"
                    self._rel_item(tree, rel_map, g, text, self._rel_target("Sensors", sid),
                                    ("SensorUnits", li))
                else:
                    self._rel_item(tree, rel_map, g, f"[SensorUnits.Id={link.get('Id')}] SensorId=0 ({tr.t('rel_not_set')})",
                                   ("SensorUnits", li))

        # 改装与选项
        mo = self._sort_by_order_id("Modifications", self._by_unit.get("Modifications", {}).get(uid, []))
        g = self._rel_group(tree, "", tr.t("rel_group_mods"))
        rel_map[g] = {"kind": "group", "group": "mods", "nav": None, "edit": None}
        if mo:
            for li in mo:
                link = self.tables["Modifications"][li]
                mid = link.get("Id")
                slot_name = localize_text(link.get("UIName") or "", self.tr.lang)
                if not slot_name:
                    slot_name = link.get("Name") or ""
                mparent = self._rel_item(tree, rel_map, g,
                                         f"🧩 {slot_name}  [Modifications.Id={mid}]",
                                         ("Modifications", li), ("Modifications", li))
                for oi in self._sort_by_order_id("Options", self._by_mod.get("Options", {}).get(mid, [])):
                    opt = self.tables["Options"][oi]
                    oid = opt.get("Id")
                    opt_name = self._display_name("Options", opt)
                    parts = [f"🧩 {opt_name}  [Options.Id={oid}]"]
                    if opt.get("IsDefault"):
                        parts.append("★" + tr.t("rel_default_mark"))
                    if isinstance(opt.get("ReplaceUnitId"), int) and opt["ReplaceUnitId"] > 0:
                        parts.append(tr.t("rel_replace_unit") + "=" +
                                     self._row_name("Units", opt["ReplaceUnitId"]))
                    nt = sum(1 for k in range(0, 21)
                             if isinstance(opt.get(f"Turret{k}Id"), int) and opt[f"Turret{k}Id"] > 0)
                    na = sum(1 for k in (1, 2, 3)
                             if isinstance(opt.get(f"Ability{k}Id"), int) and opt[f"Ability{k}Id"] > 0)
                    if nt:
                        parts.append(tr.t("rel_turrets_n", n=nt))
                    if na:
                        parts.append(tr.t("rel_abilities_n", n=na))
                    self._rel_item(tree, rel_map, mparent, "  ".join(parts),
                                    ("Options", oi), ("Options", oi),
                                    game_icon=opt.get("OptionPicture"))

    def _rel_ammo_under(self, tree, rel_map, parent, uid, wid):
        """Nest the WeaponAmmunitions bound to (unit, weapon) under a weapon
        node; each ammo row is editable via the mini editor."""
        for ai in self._sort_by_order_id("WeaponAmmunitions", self._by_unit.get("WeaponAmmunitions", {}).get(uid, [])):
            wa = self.tables["WeaponAmmunitions"][ai]
            if wa.get("WeaponId") != wid:
                continue
            aid = wa.get("AmmunitionId")
            text = "💣 " + (self._row_name("Ammunitions", aid) if isinstance(aid, int) and aid > 0 else "?")
            if isinstance(wa.get("Quantity"), (int, float)):
                text += f" ×{wa['Quantity']}"
            nav = self._rel_target("Ammunitions", aid) if isinstance(aid, int) and aid > 0 else None
            self._rel_item(tree, rel_map, parent, text, nav, ("WeaponAmmunitions", ai),
                           game_icon=self._row_field("Ammunitions", aid, "HUDIcon") if isinstance(aid, int) and aid > 0 else None)

    def _rel_member_weapon(self, tree, rel_map, parent, uid, wid, label, member_idx, field):
        """Nest a squad member's primary/secondary weapon plus its ammo."""
        tr = self.tr
        if not isinstance(wid, int) or wid <= 0:
            return
        wnode = self._rel_item(tree, rel_map, parent,
                               f"🔫 {label}: {self._row_name('Weapons', wid)}  [Weapons.Id={wid}]",
                               self._rel_target("Weapons", wid), None,
                               game_icon=self._row_field("Weapons", wid, "HUDIcon"))
        # the weapon itself is not directly deletable from here (Delete would
        # otherwise remove the Weapons row); the ammo bindings below are
        rel_map[wnode]["edit"] = None
        # unique identity: the same weapon may be shared by several members, so
        # a plain weapon key would collide and lose the fold state
        rel_map[wnode]["key"] = ("member_weapon", member_idx, field)
        self._rel_ammo_under(tree, rel_map, wnode, uid, wid)

    def _build_generic_relations(self, tree, rel_map, table, row):
        """Build the relation tree for a non-unit table.

        Shows two groups: outgoing references (this row's FK fields) and
        incoming references (rows in other tables that point at this row's Id).
        Incoming refs are capped at REL_CAP per group.
        """
        tr = self.tr
        self._ensure_indexes()
        rid = row.get("Id") if isinstance(row.get("Id"), int) else None

        out = []
        for field, val in row.items():
            if field == "Id" or not isinstance(val, int) or val <= 0:
                continue
            target = FIELD_REF_MAP.get(field)
            if target and target in self.tables:
                out.append((field, target, val))
        if out:
            g = self._rel_group(tree, "", tr.t("rel_out"))
            for field, target, val in out:
                text = f"{field} → {self._row_name(target, val)}  [{target}.Id={val}]"
                self._rel_item(tree, rel_map, g, text, self._rel_target(target, val),
                               self._rel_target(target, val))

        if isinstance(rid, int):
            inc = find_references(self.tables, table, rid)
            if inc:
                g = self._rel_group(tree, "", tr.t("rel_in"))
                for i, (tname, idx, field) in enumerate(inc[:REL_CAP]):
                    text = f"{tname} {tr.t('rel_row', idx=idx)} {field}={rid}"
                    self._rel_item(tree, rel_map, g, text, (tname, idx), (tname, idx))
                if len(inc) > REL_CAP:
                    tree.insert(g, "end", text=f"… (+{len(inc) - REL_CAP})")

    def _current_unit_id(self, owner):
        """Id of the unit currently shown in a detail window (or None)."""
        if owner is None or owner.table != "Units" or owner.row is None:
            return None
        row = self.tables["Units"][owner.row]
        return row.get("Id") if isinstance(row.get("Id"), int) else None

    def _blank_row(self, table):
        """Blank values for every field seen in `table`'s rows, so newly added
        rows match the loaded database's schema rather than a hard-coded one."""
        prio = {bool: 5, int: 4, float: 3, str: 2, type(None): 1}
        types = {}
        for r in self.tables.get(table, []):
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                cur = types.get(k)
                if cur is None or prio.get(type(v), 0) > prio.get(cur, 0):
                    types[k] = type(v)
        blank = {}
        for k, t in types.items():
            if t is bool:
                blank[k] = False
            elif t is int:
                blank[k] = 0
            elif t is float:
                blank[k] = 0.0
            elif t is str:
                blank[k] = ""
            else:
                blank[k] = None
        return blank

    def add_link_row(self, owner, table, preset):
        """Append one link row (e.g. UnitAbilities) and refresh the owner tree.
        No detail window is opened — the new row is edited inline in the
        relation panel. Returns the new row index, or None."""
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        if table not in self.tables:
            self._show_error(self.tr.t("error_title") + ": " + table)
            return None
        rows = self.tables[table]
        rid = next_free_id(rows)
        new_row = self._blank_row(table)
        new_row.update(preset)
        new_row["Id"] = rid
        old = list(rows)
        self.undo.push(table, old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if table == self.current_table:
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self._toast(self.tr.t("new_id_allocated", id=rid))
        return len(rows) - 1

    def add_unit_link(self, owner, link_table, field, value, extra=None):
        """Link the current unit to an existing id (ability/turret/weapon/armor/
        mobility/sensor)."""
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        preset = {"UnitId": uid, field: value}
        if extra:
            preset.update(extra)
        return self.add_link_row(owner, link_table, preset)

    def add_flypreset(self, owner):
        """Create a new FlyPreset and attach it to the current unit's Mobility."""
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        if "FlyPresets" not in self.tables:
            self._show_error(self.tr.t("error_title") + ": FlyPresets")
            return None
        self._ensure_indexes()

        # 1. create the new FlyPreset entry
        fps = self.tables["FlyPresets"]
        fid = next_free_id(fps)
        new_fp = self._blank_row("FlyPresets")
        new_fp.update({"Id": fid, "Name": "New FlyPreset U%d" % uid})
        old_fps = list(fps)
        self.undo.push("FlyPresets", old_fps, old_fps + [new_fp], "add")
        fps.append(new_fp)

        # 2. attach it to the current unit's first Mobility entry
        attached = False
        mobility_idx = None
        for li in self._by_unit.get("UnitPropulsions", {}).get(uid, []):
            link = self.tables["UnitPropulsions"][li]
            mid = link.get("MobilityId")
            if isinstance(mid, int) and mid > 0:
                mobility_idx = self._id_index.get("Mobility", {}).get(mid)
                if mobility_idx is not None:
                    break
        if mobility_idx is not None:
            mob_rows = self.tables["Mobility"]
            new_mob = json.loads(json.dumps(mob_rows[mobility_idx]))
            new_mob["FlyPresetId"] = fid
            old_full = list(mob_rows)
            mob_rows[mobility_idx] = new_mob
            self.undo.push("Mobility", old_full, list(mob_rows), "edit")
            attached = True

        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "FlyPresets":
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self.open_detail("FlyPresets", len(fps) - 1)
        if attached:
            self._toast(self.tr.t("new_flypreset_attached", id=fid))
        else:
            self._toast(self.tr.t("new_id_allocated", id=fid))
        return fid

    def add_mount_turret(self, owner):
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        return self.add_link_row(owner, "TurretUnits",
                                 {"UnitId": uid, "TurretId": 0, "Order": 0})

    def add_squad_member(self, owner):
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        return self.add_link_row(owner, "SquadMembers",
                                 {"UnitId": uid, "ModelFileName": None,
                                  "PrimaryWeaponId": 0, "SpecialWeaponId": 0,
                                  "DeathPriority": 0})

    def add_mod_slot(self, owner):
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        return self.add_link_row(owner, "Modifications",
                                 {"UnitId": uid, "Name": "New Slot", "Type": 0,
                                  "UIName": "Custom_Slot", "ThumbnailFileName": None,
                                  "Order": 0})

    def add_new_turret(self, owner):
        """Create a new empty Turret and mount it on the current unit."""
        uid = self._current_unit_id(owner)
        if uid is None:
            return None
        if "Turrets" not in self.tables or "TurretUnits" not in self.tables:
            self._show_error(self.tr.t("error_title") + ": Turrets/TurretUnits")
            return None
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        tro = self.tables["Turrets"]
        tid = next_free_id(tro)
        oldt = list(tro)
        new_turret = self._blank_row("Turrets")
        new_turret.update({"Id": tid, "Name": f"New Turret U{uid}"})
        self.undo.push("Turrets", oldt, oldt + [new_turret], "add")
        tro.append(new_turret)
        tuo = self.tables["TurretUnits"]
        lrid = next_free_id(tuo)
        oldu = list(tuo)
        new_link = self._blank_row("TurretUnits")
        new_link.update({"Id": lrid, "UnitId": uid, "TurretId": tid, "Order": 0})
        self.undo.push("TurretUnits", oldu, oldu + [new_link], "add")
        tuo.append(new_link)
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table in ("Turrets", "TurretUnits"):
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self.open_detail("Turrets", len(tro) - 1)
        self._toast(self.tr.t("new_id_allocated", id=tid))
        return tid

    def add_turret_weapon(self, owner, turret_id):
        """Add an empty TurretWeapons row to a turret (user fills WeaponId)."""
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        if "TurretWeapons" not in self.tables:
            self._show_error(self.tr.t("error_title") + ": TurretWeapons")
            return None
        rows = self.tables["TurretWeapons"]
        order = max([r.get("Order") for r in rows if isinstance(r, dict)
                     and r.get("TurretId") == turret_id and isinstance(r.get("Order"), int)]
                    or [-1]) + 1
        rid = next_free_id(rows)
        new_row = self._blank_row("TurretWeapons")
        new_row.update({"Id": rid, "TurretId": turret_id, "WeaponId": 0, "Order": order})
        old = list(rows)
        self.undo.push("TurretWeapons", old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "TurretWeapons":
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self._toast(self.tr.t("new_id_allocated", id=rid))
        return len(rows) - 1

    def add_option_to_mod(self, owner, mod_id):
        """Add a new Option linked to a modification slot (Modifications.Id)."""
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        if "Options" not in self.tables:
            self._show_error(self.tr.t("error_title") + ": Options")
            return None
        rows = self.tables["Options"]
        rid = next_free_id(rows)
        new_row = self._blank_row("Options")
        new_row.update({
            "Id": rid,
            "ModificationId": mod_id,
            "Name": "New Option",
            "UIName": "Custom_Option",
        })
        old = list(rows)
        self.undo.push("Options", old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "Options":
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self._toast(self.tr.t("new_id_allocated", id=rid))
        return len(rows) - 1

    def _sync_squad_weapon(self, unit_id, weapon_id):
        """Ensure a weapon is present in the unit's SquadWeapons pool (add it if
        missing). No-op for invalid/empty weapon ids."""
        if not isinstance(unit_id, int) or not isinstance(weapon_id, int) or weapon_id <= 0:
            return
        sw = self.tables.get("SquadWeapons")
        if sw is None:
            return
        exists = any(isinstance(r, dict) and r.get("UnitId") == unit_id
                     and r.get("WeaponId") == weapon_id for r in sw)
        if exists:
            return
        order = max([r.get("Order") for r in sw if isinstance(r, dict)
                     and r.get("UnitId") == unit_id and isinstance(r.get("Order"), int)]
                    or [-1]) + 1
        lrid = next_free_id(sw)
        new_link = self._blank_row("SquadWeapons")
        new_link.update({"Id": lrid, "UnitId": unit_id, "WeaponId": weapon_id, "Order": order})
        old_sw = list(sw)
        self.undo.push("SquadWeapons", old_sw, old_sw + [new_link], "add")
        sw.append(new_link)
        self._refresh_table_list()
        if self.current_table == "SquadWeapons":
            self.rebuild_tree()

    def set_squad_member_weapon(self, owner, member_idx, field, weapon_id):
        """Set a squad member's primary/secondary weapon, and keep the unit's
        SquadWeapons pool in sync (so the weapon also appears under infantry
        weapons)."""
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return False
        rows = self.tables.get("SquadMembers")
        if rows is None or not (0 <= member_idx < len(rows)):
            return False
        member = rows[member_idx]
        if not isinstance(member, dict):
            return False
        uid = member.get("UnitId")
        if not isinstance(uid, int):
            return False
        new_row = json.loads(json.dumps(member))
        new_row[field] = weapon_id
        old_full = list(rows)
        rows[member_idx] = new_row
        self.undo.push("SquadMembers", old_full, list(rows), "edit")
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "SquadMembers":
            self.rebuild_tree()
        self._sync_squad_weapon(uid, weapon_id)
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        return True

    def _global_common_ammo(self):
        """Most frequently used (ammunition, quantity, unit) across the whole
        database. Returns (ammunition_id, quantity, unit_id); (0, 1, 0) when
        there are no ammo bindings."""
        from collections import Counter, defaultdict
        ammo = Counter()
        ammo_qty = defaultdict(Counter)
        ammo_unit = defaultdict(Counter)
        for r in self.tables.get("WeaponAmmunitions", []):
            if not isinstance(r, dict):
                continue
            aid = r.get("AmmunitionId")
            if not isinstance(aid, int) or aid <= 0:
                continue
            ammo[aid] += 1
            if isinstance(r.get("Quantity"), (int, float)):
                ammo_qty[aid][r["Quantity"]] += 1
            if isinstance(r.get("UnitId"), int):
                ammo_unit[aid][r["UnitId"]] += 1
        if not ammo:
            return 0, 1, 0
        best = ammo.most_common(1)[0][0]
        qty = ammo_qty[best].most_common(1)[0][0] if ammo_qty[best] else 1
        unit = ammo_unit[best].most_common(1)[0][0] if ammo_unit[best] else 0
        return best, qty, unit

    def _common_ammo_for_weapon(self, weapon_id):
        """Most frequently used (ammunition, quantity) for a weapon across the
        whole database. Returns (0, 1) when the weapon has no ammo bindings."""
        from collections import Counter
        counts = Counter()
        qtys = {}
        for r in self.tables.get("WeaponAmmunitions", []):
            if not isinstance(r, dict) or r.get("WeaponId") != weapon_id:
                continue
            aid = r.get("AmmunitionId")
            if not isinstance(aid, int) or aid <= 0:
                continue
            counts[aid] += 1
            if isinstance(r.get("Quantity"), (int, float)):
                qtys.setdefault(aid, []).append(r["Quantity"])
        if not counts:
            return 0, 1
        best = counts.most_common(1)[0][0]
        qs = qtys.get(best, [])
        best_q = Counter(qs).most_common(1)[0][0] if qs else 1
        return best, best_q

    def _auto_add_common_ammo(self, weapon_id):
        """Auto-attach the most common ammo binding to a newly added weapon."""
        if "WeaponAmmunitions" not in self.tables:
            return
        aid, qty, unit_id = self._global_common_ammo()
        if aid <= 0:
            return
        rows = self.tables["WeaponAmmunitions"]
        rid = next_free_id(rows)
        new_row = self._blank_row("WeaponAmmunitions")
        new_row.update({"Id": rid, "UnitId": unit_id, "WeaponId": weapon_id,
                        "AmmunitionId": aid, "Order": 0, "Quantity": qty})
        old = list(rows)
        self.undo.push("WeaponAmmunitions", old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "WeaponAmmunitions":
            self.rebuild_tree()

    def _auto_add_weapon_ammo(self, unit_id, weapon_id):
        """Auto-attach a weapon's most common ammo to a unit (used after a squad
        member's primary/secondary weapon is assigned). Skips when the unit
        already has an ammo binding, or when the weapon has no common ammo."""
        if not isinstance(unit_id, int) or not isinstance(weapon_id, int) or weapon_id <= 0:
            return
        rows = self.tables.get("WeaponAmmunitions")
        if rows is None:
            return
        exists = any(isinstance(r, dict) and r.get("UnitId") == unit_id
                     and r.get("WeaponId") == weapon_id for r in rows)
        if exists:
            return
        aid, qty = self._common_ammo_for_weapon(weapon_id)
        if aid <= 0:
            return  # 该武器没有常用弹药，不自动添加
        rid = next_free_id(rows)
        new_row = self._blank_row("WeaponAmmunitions")
        new_row.update({"Id": rid, "UnitId": unit_id, "WeaponId": weapon_id,
                        "AmmunitionId": aid, "Order": 0, "Quantity": qty})
        old = list(rows)
        self.undo.push("WeaponAmmunitions", old, old + [new_row], "add")
        rows.append(new_row)
        self._refresh_table_list()

    def add_weapon_ammo(self, owner, unit_id, weapon_id, ammunition_id):
        """Add a WeaponAmmunitions binding for a weapon of the current unit.
        If no ammo is given, the weapon's most common ammo+quantity is used."""
        if owner is not None and (not owner.commit(silent=False) or not owner.commit_mini(silent=False)):
            return None
        if "WeaponAmmunitions" not in self.tables:
            self._show_error(self.tr.t("error_title") + ": WeaponAmmunitions")
            return None
        rows = self.tables["WeaponAmmunitions"]
        order = max([r.get("Order") for r in rows if isinstance(r, dict)
                     and r.get("UnitId") == unit_id and r.get("WeaponId") == weapon_id
                     and isinstance(r.get("Order"), int)] or [-1]) + 1
        if ammunition_id is None or ammunition_id <= 0:
            ammunition_id, quantity = self._common_ammo_for_weapon(weapon_id)
        else:
            quantity = 1
        rid = next_free_id(rows)
        new_row = self._blank_row("WeaponAmmunitions")
        new_row.update({"Id": rid, "UnitId": unit_id, "WeaponId": weapon_id,
                        "AmmunitionId": ammunition_id, "Order": order,
                        "Quantity": quantity})
        old = list(rows)
        self.undo.push("WeaponAmmunitions", old, old + [new_row], "add")
        rows.append(new_row)
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == "WeaponAmmunitions":
            self.rebuild_tree()
        if owner is not None:
            self._build_relations_into(owner.rel_tree, owner.rel_map, owner.table, owner.row, owner)
        self._toast(self.tr.t("new_id_allocated", id=rid))
        return len(rows) - 1

    def delete_unit_group(self, owner, group):
        """Delete every row of a relation group (fold bar) for the current unit.
        For 'mods' this also removes the Options of the deleted slots."""
        table = GROUP_LINK_TABLES.get(group)
        if table is None or table not in self.tables:
            return False
        uid = self._current_unit_id(owner)
        if uid is None:
            return False
        rows = self.tables[table]
        doomed_mods = []
        keep = []
        for r in rows:
            if isinstance(r, dict) and r.get("UnitId") == uid:
                if table == "Modifications" and isinstance(r.get("Id"), int):
                    doomed_mods.append(r["Id"])
            else:
                keep.append(r)
        if len(keep) == len(rows):
            return False
        old = list(rows)
        # remove Options belonging to the deleted modification slots
        if table == "Modifications" and doomed_mods and "Options" in self.tables:
            orows = self.tables["Options"]
            oold = list(orows)
            okeep = [r for r in orows
                     if not (isinstance(r, dict) and r.get("ModificationId") in doomed_mods)]
            if len(okeep) != len(orows):
                self.undo.push("Options", oold, okeep, "delete")
                self.tables["Options"][:] = okeep
                self._refresh_table_list()
                if self.current_table == "Options":
                    self.rebuild_tree()
        self.undo.push(table, old, keep, "delete")
        self.tables[table][:] = keep
        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table == table:
            self.rebuild_tree()
        return True

    def clone_unit(self, idx):
        """Clone a unit as a template: copies the unit row plus its turrets,
        squad, ammo bindings, abilities, armors, mobility and
        modifications+options with fresh Ids. Weapons are shared (not copied)
        so the clone reuses the original weapon rows. Returns the new unit row
        index, or None."""
        if "Units" not in self.tables:
            return None
        units = self.tables["Units"]
        if not (0 <= idx < len(units)) or not isinstance(units[idx], dict):
            return None
        self._ensure_indexes()
        src = units[idx]
        uid = src.get("Id") if isinstance(src.get("Id"), int) else None
        if uid is None:
            return None
        tables = self.tables
        changes = {}          # table -> list of new rows (Id=0 placeholders)

        def add(table, row):
            changes.setdefault(table, []).append(row)

        def new_id(table):
            mx = next_free_id(tables[table])
            for r in changes.get(table, []):
                if isinstance(r.get("Id"), int):
                    mx = max(mx, r["Id"] + 1)
            return mx

        def copy_of(table, rid):
            ci = self._id_index.get(table, {}).get(rid)
            if ci is None:
                return None
            row = tables[table][ci]
            return json.loads(json.dumps(row)) if isinstance(row, dict) else None

        turret_map = {}
        mod_map = {}

        # turrets + their weapons (weapons are shared, not cloned)
        for li in self._by_unit.get("TurretUnits", {}).get(uid, []):
            link = tables["TurretUnits"][li]
            tid = link.get("TurretId")
            if isinstance(tid, int) and tid > 0:
                if tid not in turret_map:
                    new_turret = copy_of("Turrets", tid) or {"Name": f"Turret copy of {tid}"}
                    turret_map[tid] = new_id("Turrets")
                    new_turret["Id"] = turret_map[tid]
                    add("Turrets", new_turret)
                new_link = json.loads(json.dumps(link))
                new_link["Id"] = new_id("TurretUnits")
                new_link["UnitId"] = new_link.get("UnitId")  # keep placeholder until unit id known
                new_link["TurretId"] = turret_map[tid]
                add("TurretUnits", new_link)
            for wi in self._by_turret.get("TurretWeapons", {}).get(tid, []):
                tw = tables["TurretWeapons"][wi]
                new_tw = json.loads(json.dumps(tw))
                new_tw["Id"] = new_id("TurretWeapons")
                if tid in turret_map:
                    new_tw["TurretId"] = turret_map[tid]
                # keep the original WeaponId (weapons are shared, not cloned)
                add("TurretWeapons", new_tw)

        # squad weapons (keep the original WeaponId)
        for li in self._by_unit.get("SquadWeapons", {}).get(uid, []):
            link = tables["SquadWeapons"][li]
            new_link = json.loads(json.dumps(link))
            new_link["Id"] = new_id("SquadWeapons")
            add("SquadWeapons", new_link)

        # squad members (keep the original primary/secondary weapons)
        for li in self._by_unit.get("SquadMembers", {}).get(uid, []):
            link = tables["SquadMembers"][li]
            new_link = json.loads(json.dumps(link))
            new_link["Id"] = new_id("SquadMembers")
            add("SquadMembers", new_link)

        # ammo bindings (same ammunition, keep the original WeaponId)
        for li in self._by_unit.get("WeaponAmmunitions", {}).get(uid, []):
            link = tables["WeaponAmmunitions"][li]
            new_link = json.loads(json.dumps(link))
            new_link["Id"] = new_id("WeaponAmmunitions")
            add("WeaponAmmunitions", new_link)

        # abilities / armors / mobility / sensor links (shared targets, new links)
        for tname, tf in (("UnitAbilities", "AbilityId"), ("UnitArmors", "ArmorId"),
                          ("UnitPropulsions", "MobilityId"), ("SensorUnits", "SensorId")):
            for li in self._by_unit.get(tname, {}).get(uid, []):
                new_link = json.loads(json.dumps(tables[tname][li]))
                new_link["Id"] = new_id(tname)
                add(tname, new_link)

        # modifications + options
        for li in self._by_unit.get("Modifications", {}).get(uid, []):
            link = tables["Modifications"][li]
            old_mid = link.get("Id")
            new_mod = json.loads(json.dumps(link))
            new_mid = new_id("Modifications")
            new_mod["Id"] = new_mid
            mod_map[old_mid] = new_mid
            add("Modifications", new_mod)
            for oi in self._by_mod.get("Options", {}).get(old_mid, []):
                opt = tables["Options"][oi]
                new_opt = json.loads(json.dumps(opt))
                new_opt["Id"] = new_id("Options")
                new_opt["ModificationId"] = new_mid
                add("Options", new_opt)

        # the new unit itself
        new_uid = new_id("Units")
        new_unit = json.loads(json.dumps(src))
        new_unit["Id"] = new_uid
        # fix link rows' UnitId
        for tname in ("TurretUnits", "SquadWeapons", "SquadMembers",
                      "WeaponAmmunitions", "UnitAbilities", "UnitArmors",
                      "UnitPropulsions", "SensorUnits", "Modifications"):
            for r in changes.get(tname, []):
                r["UnitId"] = new_uid

        # append everything (one undo entry per table)
        for tname, new_rows in changes.items():
            rows = tables[tname]
            old = list(rows)
            self.undo.push(tname, old, old + new_rows, "clone")
            rows.extend(new_rows)
        old_units = list(units)
        self.undo.push("Units", old_units, old_units + [new_unit], "clone")
        units.append(new_unit)

        self._mark_dirty()
        self._refresh_table_list()
        if self.current_table in changes or self.current_table == "Units":
            self.rebuild_tree()
        self._refresh_detail()
        return len(units) - 1

    # ---------------- name lookup ----------------

    def search_names(self, q):
        """Search all tables by Name / HUDName / UIName / OptionPicture /
        ThumbnailFileName / ThumbnailOverride / HUDIcon / Id.
        Returns list of (table, row_index, id, name, hud, extras) where extras
        lists the matched non-name fields as 'Field: value' strings."""
        q = q.strip().lower()
        if not q:
            return []
        results = []
        q_is_int = q.isdigit()
        for tname, rows in self.tables.items():
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                hit = False
                extras = []
                for f in LOOKUP_FIELDS:
                    v = r.get(f)
                    if isinstance(v, str) and q in v.lower():
                        hit = True
                        if f not in ("Name", "HUDName"):
                            extras.append(f"{f}: {v}")
                if not hit and q_is_int and isinstance(r.get("Id"), int) and q == str(r["Id"]):
                    hit = True
                if hit:
                    name = r.get("Name") or r.get("HUDName") or r.get("UIName") or ""
                    hud = r.get("HUDName") or ""
                    results.append((tname, i, r.get("Id"), name, hud, extras))
                    if len(results) >= 500:
                        break
            if len(results) >= 500:
                break
        return results

    def resolve_name_in_table(self, table, text):
        """Resolve a typed name / HUD name to an Id inside one table.
        Returns (id, match_count); id is None unless exactly one match."""
        q = text.strip().lower()
        if not q:
            return None, 0
        exact, sub = [], []
        for r in self.tables.get(table, []):
            if not isinstance(r, dict) or not isinstance(r.get("Id"), int):
                continue
            names = [r[k] for k in ("Name", "HUDName", "UIName")
                     if isinstance(r.get(k), str)]
            if any(n.lower() == q for n in names):
                exact.append(r["Id"])
            elif any(q in n.lower() for n in names):
                sub.append(r["Id"])
        if len(exact) == 1:
            return exact[0], 1
        if exact:
            return None, len(exact)
        if len(sub) == 1:
            return sub[0], 1
        if sub:
            return None, len(sub)
        return None, 0

    # ---------------- inline name lookup (toolbar) ----------------

    def _focus_lookup(self, _event=None):
        self.lookup_box.focus()
        return "break"

    # ---------------- references / validation ----------------

    def find_references_dialog(self):
        if self.current_row is None or self.current_table not in self.tables:
            return
        row = self.tables[self.current_table][self.current_row]
        if not isinstance(row, dict) or not isinstance(row.get("Id"), int):
            return
        rid = row["Id"]
        results = find_references(self.tables, self.current_table, rid)
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(self.tr.t("refs_title", table=self.current_table, id=rid))
        win.geometry("620x440")
        tr = self.tr
        if not results:
            tk.Label(win, text=tr.t("refs_none"), padx=12, pady=12).pack(anchor="w")
            return
        lb = tk.Listbox(win)
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for (tname, idx, field) in results:
            lb.insert("end", tr.t("refs_item", table=tname, idx=idx, field=field, id=rid))
        def jump(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            tname, idx, _field = results[sel[0]]
            self._jump_to(tname, idx)
            win.destroy()
        lb.bind("<Double-1>", jump)
        tk.Button(win, text=tr.t("goto_row"), command=jump).pack(pady=4)
        tk.Button(win, text=tr.t("close"), command=win.destroy).pack(pady=(0, 8))
        self.theme_children(win)

    def _jump_to(self, table, idx):
        self.commit_editor(silent=False)
        if table not in self.tables:
            return
        if table != self.current_table:
            self._select_table(table)
        self.current_row = idx
        self._select_row_in_tree()
        self.open_detail(table, idx)

    def validate_dialog(self):
        if not self.tables:
            self._show_warning(self.tr.t("msg_no_file"))
            return
        self.commit_editor(silent=False)
        tr = self.tr
        tk = self.tk
        issues_all = validate_db(self.tables, None)
        base_sigs = baseline_issue_sigs()
        issues_new = [it for it in issues_all
                      if issue_sig(it) not in base_sigs]
        win = tk.Toplevel(self.root)
        win.title(tr.t("validate_title"))
        win.geometry("800x500")
        show_new_only = tk.BooleanVar(value=bool(base_sigs))
        chk = tk.Checkbutton(win, text=tr.t("validate_only_new"), variable=show_new_only,
                             command=lambda: fill(issues_new if show_new_only.get() else issues_all))
        chk.pack(anchor="w", padx=8, pady=(6, 0))
        count_l = tk.Label(win, anchor="w")
        count_l.pack(anchor="w", padx=8)
        lb = tk.Listbox(win)
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        def fill(lst):
            lb.delete(0, "end")
            for it in lst:
                lb.insert("end", issue_text(it, tr))
            count_l.config(text=tr.t("validate_count", n=len(lst)))
        fill(issues_new if show_new_only.get() else issues_all)
        def current_issues():
            return issues_new if show_new_only.get() else issues_all
        def jump(_e=None):
            sel = lb.curselection()
            lst = current_issues()
            if not sel or not (0 <= sel[0] < len(lst)):
                return
            it = lst[sel[0]]
            if it.get("table") and it.get("idx") is not None:
                self._jump_to(it["table"], it["idx"])
                win.destroy()
        lb.bind("<Double-1>", jump)
        tk.Button(win, text=tr.t("goto_row"), command=jump).pack(pady=4)
        tk.Button(win, text=tr.t("close"), command=win.destroy).pack(pady=(0, 8))
        self.theme_children(win)

    # ---------------- dialogs / misc ----------------

    def _run_async(self, label, func, on_done):
        import queue
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title(self.tr.t("progress_title"))
        tk.Label(win, text=label, padx=16, pady=8).pack()
        bar = ttk.Progressbar(win, mode="indeterminate", length=280)
        bar.pack(padx=16, pady=(0, 12))
        bar.start(12)
        self.theme_children(win)
        win.transient(self.root)
        win.grab_set()
        q = queue.Queue()
        def worker():
            try:
                q.put(("value", func()))
            except Exception as e:
                q.put(("error", e))
        def poll():
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                self.root.after(120, poll)
                return
            bar.stop()
            win.destroy()
            if kind == "error":
                self._show_error(f"{self.tr.t('error_title')}: {payload}")
            else:
                on_done(payload)
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(120, poll)

    def _show_info(self, text):
        from tkinter import messagebox
        messagebox.showinfo(self.tr.t("info_title"), text, parent=self.root)

    def _show_warning(self, text):
        from tkinter import messagebox
        messagebox.showwarning(self.tr.t("warning_title"), text, parent=self.root)

    def _show_error(self, text):
        from tkinter import messagebox
        messagebox.showerror(self.tr.t("error_title"), text, parent=self.root)

    def _ask_yes_no(self, title, text):
        from tkinter import messagebox
        return messagebox.askyesno(title, text, parent=self.root)

    def _confirm_discard(self):
        if self.dirty:
            return self._ask_yes_no(self.tr.t("warning_title"), self.tr.t("msg_unsaved_open"))
        return True

    def _unit_model_map(self):
        """Every entity -> the models it uses, for the dictionary "Models" tab.

        Units resolve recursively (self + turret + turret weapons + squad
        weapons + squad members' weapons + ammo + armor/sensor/ability/
        mobility); weapons, ammo and turrets list their own model. Returns a
        list of (table, display, id, [(role_key, model_name), ...])."""
        if getattr(self, "_umm_ver", None) == self._data_version \
                and getattr(self, "_umm_lang", None) == self.tr.lang:
            return self._umm_cache
        self._ensure_indexes()
        out = []
        def disp(tname, row):
            d = self._display_name(tname, row)
            if d and d != "?":
                return d
            return row.get("Name") or row.get("HUDName") or row.get("ModelFileName") or ""
        def model_of(tname, rid):
            idx = self._id_index.get(tname, {}).get(rid)
            if idx is None:
                return None
            v = self.tables[tname][idx].get("ModelFileName")
            return v if isinstance(v, str) and v.strip() else None

        for u in self.tables.get("Units", []):
            if not isinstance(u, dict) or not isinstance(u.get("Id"), int):
                continue
            uid = u["Id"]
            seen = set()
            models = []
            def add(role, v):
                if isinstance(v, str) and v.strip() and (role, v.strip()) not in seen:
                    seen.add((role, v.strip()))
                    models.append((role, v.strip()))
            add("role_self", u.get("ModelFileName"))
            for li in self._by_unit.get("TurretUnits", {}).get(uid, []):
                tid = self.tables["TurretUnits"][li].get("TurretId")
                add("role_turret", model_of("Turrets", tid))
                for wi in self._by_turret.get("TurretWeapons", {}).get(tid, []):
                    add("role_weapon", model_of("Weapons", self.tables["TurretWeapons"][wi].get("WeaponId")))
            for li in self._by_unit.get("SquadWeapons", {}).get(uid, []):
                add("role_weapon", model_of("Weapons", self.tables["SquadWeapons"][li].get("WeaponId")))
            for li in self._by_unit.get("SquadMembers", {}).get(uid, []):
                sm = self.tables["SquadMembers"][li]
                add("role_weapon", model_of("Weapons", sm.get("PrimaryWeaponId")))
                add("role_weapon", model_of("Weapons", sm.get("SpecialWeaponId")))
            for wi in self._by_unit.get("WeaponAmmunitions", {}).get(uid, []):
                add("role_ammo", model_of("Ammunitions", self.tables["WeaponAmmunitions"][wi].get("AmmunitionId")))
            for li in self._by_unit.get("UnitArmors", {}).get(uid, []):
                add("role_armor", model_of("Armors", self.tables["UnitArmors"][li].get("ArmorId")))
            for li in self._by_unit.get("SensorUnits", {}).get(uid, []):
                add("role_sensor", model_of("Sensors", self.tables["SensorUnits"][li].get("SensorId")))
            for li in self._by_unit.get("UnitAbilities", {}).get(uid, []):
                add("role_ability", model_of("Abilities", self.tables["UnitAbilities"][li].get("AbilityId")))
            for li in self._by_unit.get("UnitPropulsions", {}).get(uid, []):
                add("role_mobility", model_of("Mobility", self.tables["UnitPropulsions"][li].get("MobilityId")))
            if models:
                out.append(("Units", disp("Units", u), uid, models))

        for tname in ("Weapons", "Ammunitions", "Turrets"):
            for r in self.tables.get(tname, []):
                if not isinstance(r, dict) or not isinstance(r.get("Id"), int):
                    continue
                v = r.get("ModelFileName")
                if isinstance(v, str) and v.strip():
                    out.append((tname, disp(tname, r), r.get("Id"), [("role_self", v.strip())]))
        self._umm_cache = out
        self._umm_ver = self._data_version
        self._umm_lang = self.tr.lang
        return out

    def show_dictionary(self, query=None):
        """Open the trilingual database dictionary window (tables + fields + enums).

        v1.8.55：query 可以是枚举键（如 "Units.Role"）——字段编辑器里的绿色箭头
        点击后用它直接在词典窗口里定位到该枚举对照表。"""
        tr = self.tr
        tk = self.tk
        c = self.c
        if getattr(self, "_dict_win", None) is not None:
            try:
                if self._dict_win.winfo_exists():
                    self._dict_win.lift()
                    self._dict_win.focus_force()
                    var = getattr(self, "_dict_search_var", None)
                    if query is not None and var is not None:
                        var.set(query)
                    return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        self._dict_win = win
        win.title(tr.t("dict_title"))
        win.geometry("960x760")
        win.minsize(640, 500)
        win.configure(bg=c["bg"])

        top = tk.Frame(win, bg=c["bg"])
        top.pack(side="top", fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text=tr.t("dict_title"), bg=c["bg"], fg=c["fg"]).pack(side="left")
        tk.Button(top, text=tr.t("close"), command=win.destroy,
                  bg=c["btn_bg"], fg=c["btn_fg"],
                  activebackground=c["btn_active"], activeforeground=c["btn_fg"],
                  padx=10).pack(side="right")

        nb = self.ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._dict_notebook = nb

        # ---- tab 1: dictionary ----
        dict_tab = tk.Frame(nb, bg=c["bg"])
        search_bar = tk.Frame(dict_tab, bg=c["bg"])
        search_bar.pack(side="top", fill="x", pady=(4, 2))
        tk.Label(search_bar, text=tr.t("dict_search") + ":", bg=c["bg"], fg=c["fg"]).pack(side="left")
        query_var = tk.StringVar()
        entry = tk.Entry(search_bar, textvariable=query_var, bg=c["entry_bg"],
                         fg=c["entry_fg"], insertbackground=c["entry_fg"])
        entry.pack(side="left", fill="x", expand=True, padx=6)
        body = tk.Frame(dict_tab, bg=c["bg"])
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, wrap="word", bg=c["tree_bg"], fg=c["tree_fg"],
                      insertbackground=c["tree_fg"], relief="flat", padx=10, pady=8,
                      font=(cjk_family(), 10), cursor="arrow")
        sb = tk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.tag_configure("table", foreground=c["accent"], font=(cjk_family(), 12, "bold"), spacing1=8)
        txt.tag_configure("enum_head", foreground=c["accent"], font=(cjk_family(), 12, "bold"), spacing1=8)
        txt.tag_configure("field", foreground=c["label_key"], font=(cjk_family(), 10, "bold"))
        txt.tag_configure("dim", foreground=c["hint"])
        txt.tag_configure("val", foreground=c["fg"])
        # v1.8.55：枚举的「真实作用」说明用绿色突出（与字段编辑器的绿色箭头同色）
        txt.tag_configure("enum_note", foreground=c["ok"])

        def render():
            lang = self.tr.lang
            q = query_var.get().strip().lower()
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            shown = 0

            for table in TABLES:
                t_name, t_desc = table_info(table, lang)
                t_name_zh, t_desc_zh = table_info(table, "zh")
                fields = ORDER.get(table) or list(FIELDS.get(table, {}).keys())
                matched = []
                for f in fields:
                    f_name, f_desc = field_info(table, f, lang)
                    f_name_zh, f_desc_zh = field_info(table, f, "zh")
                    if not q or q in str(table).lower() or q in str(t_name).lower() \
                            or q in str(t_desc).lower() or q in str(t_name_zh).lower() \
                            or q in str(t_desc_zh).lower() or q in str(f).lower() \
                            or q in str(f_name).lower() or q in str(f_desc).lower() \
                            or q in str(f_name_zh).lower() or q in str(f_desc_zh).lower():
                        matched.append((f, f_name, f_desc))
                if not matched:
                    continue
                shown += 1
                paren_l, paren_r = ("（", "）") if lang == "zh" else ("(", ")")
                if t_name and t_name != table:
                    txt.insert("end", "%s%s%s%s\n" % (table, paren_l, t_name, paren_r), "table")
                else:
                    txt.insert("end", table + "\n", "table")
                if t_desc:
                    txt.insert("end", t_desc + "\n", "dim")
                for f, f_name, f_desc in matched:
                    line = "  " + f
                    if f_name:
                        line += "  —  " + f_name
                    txt.insert("end", line + "\n", "field")
                    if f_desc:
                        txt.insert("end", "      " + f_desc + "\n", "dim")
                txt.insert("end", "\n")

            enum_shown = False
            for enum_field in ENUMS:
                vals = enum_values(enum_field, lang)
                vals_zh = enum_values(enum_field, "zh")
                note = enum_note(enum_field, lang) or enum_note(enum_field, "zh")
                if not q or q in enum_field.lower() or any(
                        q in str(v).lower() or q in str(m).lower() for v, m in vals) \
                        or any(q in str(v).lower() or q in str(m).lower() for v, m in vals_zh) \
                        or (note and q in note.lower()):
                    if not enum_shown:
                        txt.insert("end", self.tr.t("dict_enum_header") + "\n", "enum_head")
                        enum_shown = True
                    txt.insert("end", enum_field + "\n", "field")
                    if note:
                        # 该枚举的「真实作用」（反编译实证）
                        txt.insert("end", "  " + note + "\n", "enum_note")
                    for v, m in vals:
                        member = enum_member(enum_field, v)
                        line = "  " + str(v) + "  " + self.tr.t("enum_arrow") + "  " + str(m)
                        if member:
                            line += "   [" + member + "]"
                        txt.insert("end", line + "\n", "val")
                    txt.insert("end", "\n")
                    shown += 1

            if shown == 0:
                txt.insert("end", self.tr.t("dict_no_result"), "dim")
            txt.configure(state="disabled")

        # ---- tab 2: models ----
        model_tab = tk.Frame(nb, bg=c["bg"])
        msearch = tk.Frame(model_tab, bg=c["bg"])
        msearch.pack(side="top", fill="x", pady=(4, 2))
        tk.Label(msearch, text=tr.t("dict_search") + ":", bg=c["bg"], fg=c["fg"]).pack(side="left")
        model_query_var = tk.StringVar()
        mentry = tk.Entry(msearch, textvariable=model_query_var, bg=c["entry_bg"],
                          fg=c["entry_fg"], insertbackground=c["entry_fg"])
        mentry.pack(side="left", fill="x", expand=True, padx=6)
        mbody = tk.Frame(model_tab, bg=c["bg"])
        mbody.pack(fill="both", expand=True)
        mtxt = tk.Text(mbody, wrap="word", bg=c["tree_bg"], fg=c["tree_fg"],
                       insertbackground=c["tree_fg"], relief="flat", padx=10, pady=8,
                       font=(cjk_family(), 10), cursor="arrow")
        msb = tk.Scrollbar(mbody, command=mtxt.yview)
        mtxt.configure(yscrollcommand=msb.set)
        msb.pack(side="right", fill="y")
        mtxt.pack(side="left", fill="both", expand=True)
        mtxt.tag_configure("model_head", foreground=c["accent"],
                           font=(cjk_family(), 12, "bold"), spacing1=6)
        mtxt.tag_configure("val", foreground=c["fg"])
        mtxt.tag_configure("dim", foreground=c["hint"])

        def model_render():
            lang = self.tr.lang
            q = model_query_var.get().strip().lower()
            mtxt.configure(state="normal")
            mtxt.delete("1.0", "end")
            if not q:
                mtxt.insert("end", tr.t("model_hint"), "dim")
                mtxt.configure(state="disabled")
                return
            ents = self._unit_model_map()
            shown = 0
            for table, disp, rid, models in ents:
                if q not in str(disp).lower() and q not in str(rid).lower() \
                        and not any(q in m.lower() for _, m in models):
                    continue
                shown += 1
                tl = table_info(table, lang)
                tl = (tl[0] if tl else None) or table
                mtxt.insert("end", "%s  (Id=%s)  [%s]\n" % (disp, rid, tl), "model_head")
                for role, m in models:
                    rl = INFO_LABEL[role].get(lang, INFO_LABEL[role]["en"])
                    mtxt.insert("end", "  %s: %s\n" % (rl, m), "val")
                mtxt.insert("end", "\n")
            if shown == 0:
                mtxt.insert("end", tr.t("dict_no_result"), "dim")
            mtxt.configure(state="disabled")

        # ---- tab 3: mount points & templates (removed — knowledge lives in the Blender addon's dictionary)
        self._dict_render = render
        self._model_render = model_render
        self._dict_search_var = query_var
        query_var.trace_add("write", lambda *a: render())
        model_query_var.trace_add("write", lambda *a: model_render())
        nb.add(dict_tab, text=tr.t("dict_title"))
        nb.add(model_tab, text=tr.t("dict_tab_models"))
        if query:
            # 从字段编辑器的绿色箭头跳进来：直接在词典里定位该枚举
            query_var.set(query)
        render()
        model_render()
        entry.focus_set()

    def show_about(self):
        self._show_info(self.tr.t("about_text"))

    def show_help(self):
        self._show_info(self.tr.t("help_text"))

    def on_close(self):
        if self.dirty and not self._ask_yes_no(self.tr.t("warning_title"), self.tr.t("msg_unsaved")):
            return
        self.close_detail()
        self.root.destroy()
# CLI
# ---------------------------------------------------------------------------

def cli(args):
    tr = Translator()
    env_lang = os.environ.get("BA_LANG", "")
    lang = getattr(args, "lang", None) or env_lang or "zh"
    tr.set(lang if lang in LANGS else "zh")

    def load_tables_from_input(path):
        if os.path.isdir(path):
            tables, missing = import_folder(path)
            if missing:
                print(tr.t("cli_warn_missing", missing=", ".join(missing)))
            return tables
        obj = load_dump(path)
        tables, errors = decrypt_tables(obj)
        if errors:
            for k, e in errors:
                print(tr.t("cli_warn_decrypt", table=k, error=e))
        return tables

    if args.command == "decrypt":
        if not args.output:
            args.output = os.path.splitext(args.input)[0] + "_tables"
        tables, errors, written = export_folder(load_dump(args.input), args.output)
        print(tr.t("cli_decrypted", n=written, folder=args.output))
        for k, e in errors:
            print(tr.t("cli_warn_decrypt", table=k, error=e))
        return 0

    if args.command == "encrypt":
        template = load_dump(args.template)
        tables, missing = import_folder(args.input)
        if missing:
            print(tr.t("cli_warn_missing", missing=", ".join(missing)))
        out = encrypt_tables(template, tables)
        write_dump(args.output, out)
        print(tr.t("cli_encrypted", path=args.output))
        return 0

    if args.command in ("validate", "tables", "find"):
        tables = load_tables_from_input(args.input)
        if args.command == "tables":
            for name in [table_name(f) for f in TABLE_FIELDS]:
                if name in tables:
                    print(tr.t("cli_table_rows", name=name, n=len(tables[name])))
            return 0
        if args.command == "validate":
            issues_all = validate_db(tables, None)
            sigs = baseline_issue_sigs()
            show_all = getattr(args, "full", False) or not sigs
            issues = issues_all if show_all else [it for it in issues_all
                                                  if issue_sig(it) not in sigs]
            if getattr(args, "json_out", False):
                print(json.dumps(issues, ensure_ascii=False, indent=2))
            else:
                if not issues:
                    print(tr.t("validate_ok"))
                for it in issues:
                    print(" - " + issue_text(it, tr))
                print(tr.t("validate_count", n=len(issues)))
                if not show_all:
                    print(tr.t("cli_filtered_note", new=len(issues), total=len(issues_all)))
            return 0 if not issues else 1
        if args.command == "find":
            table = args.table
            rid = args.id
            results = find_references(tables, table, rid)
            if getattr(args, "json_out", False):
                print(json.dumps([{"table": t, "idx": i, "field": f} for t, i, f in results],
                                 ensure_ascii=False, indent=2))
            else:
                if not results:
                    print(tr.t("refs_none"))
                for t, i, f in results:
                    print(tr.t("refs_item", table=t, idx=i, field=f, id=rid))
            return 0

    print(tr.t("cli_usage"))
    return 2


def build_parser():
    p = argparse.ArgumentParser(prog="ba_db_tool", add_help=True,
                                description="Broken Arrow Database Editor")
    p.add_argument("--lang", choices=LANGS, default=None, help="UI/CLI language")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("gui", help="launch the GUI (default)")
    d = sub.add_parser("decrypt", help="decrypt a dump JSON to a folder of table JSONs")
    d.add_argument("input")
    d.add_argument("-o", "--output")
    e = sub.add_parser("encrypt", help="encrypt a folder of table JSONs into a dump JSON")
    e.add_argument("input")
    e.add_argument("-t", "--template", required=True, help="original UABEA dump JSON (template)")
    e.add_argument("-o", "--output", required=True)
    v = sub.add_parser("validate", help="validate a dump JSON or folder")
    v.add_argument("input")
    v.add_argument("--json", dest="json_out", action="store_true")
    v.add_argument("--full", action="store_true", help="show all issues, including clean-DB baseline ones")
    t = sub.add_parser("tables", help="list tables and row counts")
    t.add_argument("input")
    f = sub.add_parser("find", help="find rows referencing a table Id")
    f.add_argument("input")
    f.add_argument("table")
    f.add_argument("id", type=int)
    f.add_argument("--json", dest="json_out", action="store_true")
    return p


def _enable_windows_dpi_awareness():
    """Fix blurry text on high-DPI (scaled) Windows displays.

    Without DPI awareness, Windows renders the window at 96 DPI and then
    bitmap-stretches it to the display scale (125% / 150% / ...), which makes
    all text look fuzzy. Enabling DPI awareness makes the window render at the
    native resolution. Must run BEFORE tkinter creates the first window.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        for attempt in (
            lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),  # Per-Monitor v2
            lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),  # system aware
            lambda: ctypes.windll.user32.SetProcessDPIAware(),
        ):
            try:
                attempt()
                break
            except Exception:
                continue
    except Exception:
        pass


def _apply_tk_scaling(root):
    """Set Tk's scaling to the real system DPI so fonts are rendered crisp."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi and dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass


def main():
    _enable_windows_dpi_awareness()
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "command", None) in (None, "gui"):
        import tkinter as tk
        root = tk.Tk()
        _apply_tk_scaling(root)
        app = EditorApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_close)
        root.mainloop()
        return 0
    return cli(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
