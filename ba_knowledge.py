# -*- coding: utf-8 -*-
# rva-pin-audit: allow
#   ↑ 本文件**就是**钉子表的一部分（"记录当时的 RVA"），不是"抄地址来用"。
#     成对存 `key`(名字，**权威**) + `rva`(记录值)，并由
#     `测试\test_knowledge_facts.py` ② 逐条按名字当场解析复核 ⇒ 地址位移会被立刻发现。
#     普通产品/工具代码**不许**开这个口子（`测试\test_rva_pin.py` ⑦ 会扫）。
r"""**逆向事实钉板**（F2~F9）：把 2026-09-14/15 夜坐实的结论写成**可被代码引用**的结构。

为什么单独一个模块
==================
这些结论以前只活在文档里，于是同一类错会反复犯：
  · 画质开关的**极性写反**（F2）—— 文档改了，代码里的提示还是旧的
  · 命中盒**轴序搞错**（F3）—— 改错轴 ⇒ "隔空被打/打不中"
  · 方向装甲**被旁路**（F4）—— 作者以为在调四向装甲，其实那 8 列根本没参与结算
  · 权威表搞错（F5）、关联表加行不生效（F6）、音频按"哈希"找事件（F7）、
    机库看不见却不知道有且只有三种原因（F8）、拿调用图 0 命中当"没人用"（F9）
⇒ 现在它们变成 `FACTS` 表 + 供 UI/诊断直接调用的 `hint()`，
  并由 `测试\test_knowledge_facts.py` 用**钉住的 RVA + 真实 DB 导出**当裁判逐条复核。

★★ 规矩：本模块里的每条 `rva` 只是"记录当时的值"，**判据一律按名字**（`rva_key`
   指向 `技术资料\scripts\rva_pins.json` 里的钉子）—— 游戏更新会让 RVA 整体位移，
   而位移**不是常数**（1.2.0.3 实测 −0x12890 ~ +0x12160）⛔ 错地址**不报错**。

三语提示：`hint(key, lang)` 返回 zh/en/ru。新加文案时三语都要给（缺了回退 zh）。
"""

# ── 画质（F2）───────────────────────────────────────────────────────────────
MODELS_QUALITY = {
    "setting_index": 0x1D,          # SettingType.ModelsQuality
    "on_when": "<= 1",              # ★ 关键极性
    # 枚举真值：Custom(0) / Potato(1) ⇒ 渲染器**开**；Low(2) 及以上 ⇒ **关**
    "visible_values": {0: "Custom", 1: "Potato"},
    "hidden_from": 2,               # Low 起就关
}

# ── 命中盒（F3）─────────────────────────────────────────────────────────────
HITBOX = {
    # ★ 轴序真值（UnitComponent..ctor 逐行）：RawSize = (X=Width, Y=Height, Z=Length)
    "raw_size_order": ("Width", "Height", "Length"),
    "unit_offsets": {"Length": 0x88, "Width": 0x8C, "Height": 0x90},
    # 步兵例外：走组件自带 SquadCollider，**改 DB 的 Length/Width/Height 无效**
    "infantry_exception": True,
    # 固定翼 + 非制导弹药：命中盒加肥
    "aircraft_unguided_extra": "BattleSystemSettings.ADDITIONAL_PLANE_SIZE_FOR_UNGUIDED_SHELLS(0x164)",
    "aps_scale": "Ammunitions.APSHitboxProportion(0x5C)（CanBeIntercepted 且 APSCount>0 且冷却<=0）",
}

# ── 装甲（F4）───────────────────────────────────────────────────────────────
ARMOR = {
    # ★★ 旁路规则：CurrentArmor.ArmorValue > 0 ⇒ 四向×热/动能 8 列完全不参与结算
    "bypass_field": ("Units", "CurrentArmor", 0xE0),
    "bypass_offset": ("Armors", "ArmorValue", 0x28),
    "bypass_rule": "ArmorValue > 0  ⇒ 直接返回 ArmorValue，8 列不参与",
    # 8 列映射（代码级坐实）
    "columns": {
        "Kinetic": {"Front": 0x40, "Sides": 0x48, "Back": 0x44, "Top": 0x4C},
        "HEAT": {"Front": 0x30, "Sides": 0x38, "Back": 0x34, "Top": 0x3C},
    },
    "side_thresholds_deg": (45.0, 135.0),   # 生效路径 GetArmorSideFromShotVector（度，已坐实）
    "dead_code_warning": "HitDetectionSystem.GetHitSide 是**死代码**（0 调用者/0 取址），别照它推结论",
}

# ── 炮塔武器（F5）────────────────────────────────────────────────────────────
TURRET_WEAPONS = {
    "authoritative_table": "TurretWeapons",
    "overwritten_columns": ("WeaponChannel", "WeaponPriority"),
    "note": "Weapons 表 804/804 这两列全是 0，装载期被 TurretWeapons 覆盖 ⇒ 改 Weapons 那两列无效",
    "order_rule": "Order = 目标槽位下标；加/换炮塔武器要改 TurretWeapons 且 Order 要**稠密**",
    "loader": "DataBase.LoadUnits.LoadWeapons",
}

# ── 关联表（F6）──────────────────────────────────────────────────────────────
RELATION_TABLES = {
    "tables": ("UnitArmors", "UnitAbilities", "SensorUnits", "UnitPropulsions", "TurretUnits",
               "TurretWeapons", "SquadWeapons", "WeaponAmmunitions"),
    "loaded_once_at": "装载期（DataBase.LoadUnits.*），运行期没有任何 ECS 查库",
    "rules": ("加行必须让**过滤键**（UnitId / WeaponId / TurretId…）命中，否则「表里有行、游戏里没有」且不报错",
              "改表**不热生效**：要新开一局 / 重建单位"),
    "quantity_two_hops": "WeaponAmmunitions.Quantity → Weapons.WeaponAmmunition(0xB8) Dict<long,int> → "
                         "WeaponComponent..ctor(0x80F490) 建 AmmoReservedInMagazine(0xB8)",
}

# ── 音频（F7）───────────────────────────────────────────────────────────────
AUDIO = {
    "identity": "资产侧路径字符串 + bank 侧 GUID（**没有「路径哈希」这回事**）",
    "table": "Master.strings.bank 的字符串表；真值 = 技术资料/data/fmod_oracle.json（4,226 条）",
    "silent_failure": "事件名不在 bank ⇒ **无声且不报错**",
    "ui_two_layers": "UI 音 = 代码默认（AudioConfig..ctor 里 26 条 event:/ 字面量）+ 资产覆盖",
}

# ── 机库 / UI（F8）───────────────────────────────────────────────────────────
HANGAR = {
    "card_order": ("CountryId", "GetSpecializationIds", "名称(string)", "Type"),
    "search_strips_symbols": True,      # TakeOnlyLetterOrDigitSymbols ⇒ 名字带空格/连字符就搜不到
    "invisible_causes": ("筛选", "视口裁剪（UpdateCardVisibility）", "DLC 门控（ContentMembership）"),
    "uirepo_error": "日志 `Could not find UIElement by model type <X>` = 该 model 类型没在 "
                    "UIRepository._uiElementsMapping 登记（不是 DI/prefab 问题）",
}

# ── 调用图（F9）──────────────────────────────────────────────────────────────
CALLGRAPH = {
    "traps": ("who_calls 0 命中 ≠ 没调用者（可能内联）",
              "也可能走**字典/委托间接调用**（例：IncomingMessage 的处理函数在字典里）",
              "取址（find_xrefs）0 处也要一起看；三者都 0 才敢说「没有消费者」"),
    "example": "GetHitSide 三重都是 0 ⇒ 死代码；IncomingMessage 的 15 个 case 里只有 4 个直接 call",
}

# ★ 事实 → 钉子（`rva_pins.json` 的 key）。`rva` 只是当次记录值，判据按名字。
FACTS = [
    {"id": "F2", "key": "weapon_renderers_activity", "rva": 0x3715B0,
     "title": "武器 prefab 渲染器开关：ModelsQuality <= 1 才「开」",
     "data": MODELS_QUALITY},
    {"id": "F2b", "key": "weapon_prefab_awake", "rva": 0x371330,
     "title": "WeaponPrefabInfo.Awake 订阅 SettingsAppliedEvent（所以 who_calls 查不到调用者）",
     "data": MODELS_QUALITY},
    {"id": "F3", "key": "hitbox_getunitbox", "rva": 0x7D6A10,
     "title": "命中盒 = DB Units.Length/Width/Height，轴序 (Width, Height, Length)",
     "data": HITBOX},
    {"id": "F3b", "key": "unitcomponent_ctor", "rva": 0x2AD190,
     "title": "UnitComponent..ctor：RawSize = (X=Width, Y=Height, Z=Length)",
     "data": HITBOX},
    {"id": "F4", "key": "armor_by_side_and_type", "rva": 0x7B61E0,
     "title": "方向装甲旁路：CurrentArmor.ArmorValue > 0 ⇒ 8 列不参与结算", "data": ARMOR},
    {"id": "F4b", "key": "hit_side_from_shot_vector", "rva": 0x7B63C0,
     "title": "生效的侧面判定 = 局部水平角 45°/135° 分档（GetHitSide 是死代码）", "data": ARMOR},
    {"id": "F5", "key": "loadunits_loadweapons", "rva": 0x290CF0,
     "title": "TurretWeapons 才是权威表（Weapons 的 WeaponChannel/Priority 装载期被覆盖）",
     "data": TURRET_WEAPONS},
    {"id": "F9", "key": "hitdetection_update", "rva": 0x7D79E0,
     "title": "命中落地走 EntityCommandRecorder（间接），拿调用图 0 命中当结论会错",
     "data": CALLGRAPH},
]

# ★ 用户可见提示（三语）。key 与上面 F* 对应，UI/诊断直接取用。
HINTS = {
    "quality_polarity": {
        "zh": "画质「3D 模型质量」= Custom / Potato 时武器模型**可见**；Low 及以上会**整片关掉**武器 prefab 的渲染器"
              "（判据：ModelsQuality <= 1 才开）。模型「消失」先看这一项，别先怀疑资源地址。",
        "en": "With 3D model quality set to Custom/Potato the weapon model IS visible; Low and above "
              "switch off all renderers of the weapon prefab (rule: ModelsQuality <= 1 turns them ON). "
              "Check this before blaming asset paths.",
        "ru": "При качестве моделей Custom/Potato модель оружия ВИДНА; Low и выше отключают все "
              "рендереры префаба оружия (правило: ModelsQuality <= 1 включает их).",
    },
    "hitbox_axis": {
        "zh": "命中盒取自 DB 的 `Units.Length/Width/Height`，轴序是 **(X=Width, Y=Height, Z=Length)**；"
              "改模型**不会**改命中盒。步兵例外（走 SquadCollider，改 DB 无效）。",
        "en": "The hitbox comes from DB `Units.Length/Width/Height`, axis order **(X=Width, Y=Height, Z=Length)**; "
              "editing the model does NOT change it. Infantry is the exception (uses SquadCollider).",
        "ru": "Хитбокс берётся из БД `Units.Length/Width/Height`, порядок осей "
              "**(X=Width, Y=Height, Z=Length)**; правка модели его НЕ меняет.",
    },
    "armor_bypass": {
        "zh": "⛔ 想让**方向装甲**（四向 × 热/动能 8 列）生效，该行的 `ArmorValue` 必须为 0 或负；"
              "`ArmorValue > 0` 时游戏直接返回它，那 8 列**完全不参与结算**。",
        "en": "For directional armour (4 sides x KE/CE) to matter the row's `ArmorValue` must be 0 or negative; "
              "when `ArmorValue > 0` it is returned directly and those 8 columns are ignored.",
        "ru": "Чтобы направленная броня работала, `ArmorValue` строки должен быть 0 или меньше; "
              "при `ArmorValue > 0` возвращается он, а 8 столбцов игнорируются.",
    },
    "turret_authoritative": {
        "zh": "加/换**炮塔武器**请改 `TurretWeapons`（`Weapons` 的 WeaponChannel/WeaponPriority 装载期会被覆盖，改了无效），"
              "并把 `Order` 写成**稠密**的槽位下标。",
        "en": "Add/change turret weapons in `TurretWeapons` (WeaponChannel/WeaponPriority on `Weapons` are "
              "overwritten at load time), and keep `Order` dense.",
        "ru": "Оружие башни меняйте в `TurretWeapons` (колонки на `Weapons` перезаписываются при загрузке), "
              "`Order` держите плотным.",
    },
    "relation_not_hot": {
        "zh": "关联表加行：过滤键（UnitId / WeaponId / TurretId…）必须命中，否则「表里有行、游戏里没有」**且不报错**；"
              "而且**不热生效** —— 要新开一局。",
        "en": "When adding rows to relation tables the filter key must match, otherwise the row exists in the DB "
              "but not in-game, silently. Changes are NOT hot-reloaded: start a new match.",
        "ru": "При добавлении строк ключ фильтра должен совпадать, иначе строка есть в БД, но не в игре — молча. "
              "Изменения НЕ применяются на лету: начните новый бой.",
    },
    "audio_no_hash": {
        "zh": "音频事件的真实身份 = **资产侧路径字符串 + bank 侧 GUID**（不存在「路径哈希」）；"
              "事件名不在 bank 里就会**无声且不报错**。",
        "en": "An FMOD event's identity is the asset-side path string plus the bank-side GUID "
              "(there is no path hash). A name absent from the bank is silent with no error.",
        "ru": "Идентичность события FMOD — путь на стороне ассета + GUID в банке (хеша пути нет). "
              "Отсутствующее в банке имя даёт тишину без ошибок.",
    },
    "hangar_invisible": {
        "zh": "机库里「看不见」有且只有三种原因：**筛选**（搜索会去掉符号，名字带空格/连字符就搜不到）/ "
              "**视口裁剪** / **DLC 门控**。日志 `Could not find UIElement by model type <X>` 表示该 model "
              "类型没在 `UIRepository` 登记。",
        "en": "In the hangar there are exactly three reasons for invisibility: the filter (search strips symbols, "
              "so names with spaces/hyphens are unfindable), viewport culling, or DLC gating. "
              "`Could not find UIElement by model type <X>` means the model type is not registered in UIRepository.",
        "ru": "В ангаре ровно три причины невидимости: фильтр (поиск убирает символы), отсечение по вьюпорту "
              "или DLC-гейт. `Could not find UIElement by model type <X>` = тип не зарегистрирован в UIRepository.",
    },
    "callgraph_triple": {
        "zh": "「没人用」要三重验证：**字节级调用扫描** + **取址扫描** + **内联特征**；"
              "只看调用图 0 命中会漏掉内联与**字典/委托间接调用**。",
        "en": "Prove 'nobody uses it' three ways: byte-level call scan + address-taken scan + inlining signature. "
              "Callgraph-only zero hits miss inlining and dictionary/delegate indirection.",
        "ru": "«Никто не использует» доказывайте тремя способами: побайтовый скан вызовов, скан взятия адреса, "
              "признаки инлайна. Только граф вызовов пропускает инлайн и косвенные вызовы.",
    },
}


def hint(key, lang="zh"):
    """取三语提示（缺该语言回退 zh；key 不存在回退空串，**不抛异常**——它会被 UI 调用）"""
    d = HINTS.get(key) or {}
    return d.get(lang) or d.get("zh") or ""


def facts_by_id(fid):
    return [f for f in FACTS if f["id"] == fid]


def all_rva_keys():
    """本项目声明的全部钉子 key（供自检脚本核对 `rva_pins.json` 是否齐）"""
    return [f["key"] for f in FACTS]


if __name__ == "__main__":
    for f in FACTS:
        print("%-4s %-28s 0x%-8X %s" % (f["id"], f["key"], f["rva"], f["title"]))
    print("\n提示条目：%d 条 / 三语齐全：%s"
          % (len(HINTS), all(len(v) == 3 for v in HINTS.values())))
