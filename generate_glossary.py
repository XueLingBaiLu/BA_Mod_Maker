# -*- coding: utf-8 -*-
"""Generate the Broken Arrow database glossary (数据库词典.md + database_glossary.json).

v1.6.0 起的数据源（单一事实来源）：
  - ba_glossary.py              表/字段/枚举的中英俄释义（GUI 悬浮提示与词典窗口同源，
                                枚举为 dump.cs 实证值）
  - blender_addon/mount_dict.py 挂载点分类、组件
  - clean_baseline.json         行数统计（相对脚本目录）
  - 本文档内的 DATA_STRUCTURES / ADDRESS_MAP / CONSTANTS / RULES
    —— 数据结构、地址映射、常量与防崩溃铁律（原技术总结/教程知识全部收纳于此）

用法：python generate_glossary.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "blender_addon"))

import ba_glossary as BG
from mount_dict import MOUNT_CATEGORIES, COMPONENTS

with open(os.path.join(HERE, "clean_baseline.json"), "r", encoding="utf-8-sig") as f:
    DATA = json.load(f)


def c(s):
    return "`" + s + "`"


# 词典内表顺序（与历史版本一致）
TABLE_ORDER = [
    "Countries", "Units", "Weapons", "Ammunitions", "Armors", "Mobility", "FlyPresets",
    "Sensors", "Turrets", "Abilities", "Modifications", "Options", "Specializations",
    "UnitAbilities", "UnitPropulsions", "UnitArmors", "SensorUnits", "TurretUnits",
    "TurretWeapons", "WeaponAmmunitions", "SquadMembers", "SquadWeapons",
    "SpecializationAvailabilities", "TransportAvailabilities",
]

# ---------------------------------------------------------------------------
# 数据结构与文件格式（原技术总结 + 逆向知识库收纳）
# ---------------------------------------------------------------------------
DATA_STRUCTURES = """## 数据结构与文件格式

### 数据库加密（DataBaseCompiled）

- 资产：`data.unity3d` 内 `DataBaseCompiled`（UABEA 路径 51978），24 张表 JSON 字段。
- 每个表字段 = `base64("fhk3s0g3"(8B 魔数) + IV(16B 随机) + AES-256-CBC(明文JSON, PKCS#7))`。
- 密钥 = ASCII `09234237536700238099172758697347`（32 字节）；每次加密用新随机 IV。
- 明文 = 紧凑 JSON 数组（Unity JsonUtility 风格）。
- 加载路径：游戏按 `Resources/DataBaseCompiled` 从 data.unity3d 加载
  （类 BrokenArrow.Shared.Ecs.DataBaseCompiled，`DeserializeCrypt<T>` 为其解密入口）。

### AssetBundle（UnityFS）

- 容器：magic `UnityFS` + u32 格式版本(8) + 版本串 + u64 总大小 + 块信息大小×2 + flags（大端）。
- flags：0x3F=压缩类型（0 无/1 lzma/2 lz4/3 lz4hc），0x40=块信息含目录，0x80=块信息在文件尾，
  0x200=块信息前对齐。游戏实测：77 个 bundle 用 0x243（LZ4HC），units bundle 独用
  0xC2（LZ4 + 块信息在文件尾，41,695 块）。
- 数据按 128KB 分块；compSize==uncompSize 表示该块未压缩。
- 序列化文件 v22：对象目录（metadata）在文件开头——只解压首块即可枚举对象。
- 对象用 pathID(int64) 标识；PPtr = {m_FileID:i32, m_PathID:i64}。

### CRC（已破解，全量验证）

- **bundle CRC = zlib.crc32(所有数据块解压后的字节按序拼接)**，不是对文件字节算。
- catalog.json 的 AssetBundleRequestOptions 里 `m_Crc` 存十进制。
- 改任何 bundle 内容后必须重算并写回 catalog：
  `python compute_bundle_crc.py --update-catalog <aa\\catalog.json>`。
- 实测：units bundle CRC=3612175247，78 个 bundle 中 77 个与 catalog 100% 一致。
- 游戏校验失败时 GameLogs 报
  `CRC Mismatch. Provided <hex>, calculated <hex> from data. Will not load AssetBundle '<名>'`
  —— 日志里的 calculated 值可直接写回 catalog。

### catalog.json（Addressables 1.19+ 新格式）

- 4 个 base64 二进制字段：m_KeyDataString / m_BucketDataString / m_EntryDataString /
  m_ExtraDataString；字节标签对象：0=ASCII 1=Unicode(UTF-16LE) 2=UInt16 3=UInt32 4=Int32
  5=Hash128 6=Type 7=JsonObject(程序集名+类名+UTF-16LE JSON)。
- entry = 7×int32：0=InternalId 索引、1=Provider 索引、2=DependencyKey（依赖集键索引）、
  3=DepHash、4=ExtraData 对象偏移、5=PrimaryKey、6=ResourceType。
- 资产条目（BundledAssetProvider）的 InternalId 是**资产路径**；所在 bundle 经 DependencyKey
  解析——依赖集键（字符串负哈希，如 "-1968431308"）的 bucket 第一个条目 = 所在 bundle。
- 文件名锚点 = AssetBundleRequestOptions.m_Hash（文件名 = `<名字>_<m_Hash>.bundle`）。
- 规模：28,938 键、21,275 条目、14,455 InternalIds、77 个 bundle。

### .bamod 素材包

- zip（manifest.json）：`{format:"bamod-assets", version:1, bundle, prefab_path, root_pid,
  preload:[MonoScript 常量 pid + 对象 pid], objects:[{pid, class_id, script_id, tree_hash,
  raw(b64)}]}`；纹理模式另含 `textures:[{path, png(b64)}]`；皮肤模式含 `skin:{...}`
  （见「载具皮肤」节；皮肤包不清理容器条目）。
- 导入：清理旧 mod 对象（pid 段 0x4355424500000000~+0x10000）+ 旧容器/preload 条目
  （含 preloadIndex 重映射）→ 类型匹配（script_id→tree_hash→class_id）→ 新 pid 单遍字节重映射
  → 补容器条目 + preload（MonoScript 前置！）→ lz4 保存 → 重算 CRC → 可选注册地址。

### 音频（FMOD 音库）

- 游戏音频全部在 `BrokenArrow_Data/StreamingAssets` 的 FMOD Studio 音库
  （`*.bank`：RIFF-FMT-LIST-SND 结构，新版 FMOD，无 FSB5 标记；流式音频 `*.gts/*.gtp`）；
  bundle 内无 AudioClip，不走 .bamod 管线。
- StreamingAssets 无 CRC 校验 → 音库整文件替换即可生效；BA_Mod_Maker
  文件 →「导入音频/音效」做列表/备份/替换/还原/批量（备份在 `_ba_audio_backup/`）。
- 样本级编辑（提取/重混单个音效）需解析 FMOD Studio 银行元数据，属独立逆向项目。

### IL2CPP 组件（prefab 侧）

- 开发方 Steel Balalaika Studio；主程序集 BrokenArrow.dll（BrokenArrow / BrokenArrow.Client.Ecs /
  Shared.Ecs / DataBase.Models / MissionEditor / ScriptEngine 命名空间）。
- MonoBehaviour 序列化 = m_GameObject PPtr(12B) + m_Enabled(1B)+pad(3B) + m_Script PPtr(12B)
  + m_Name 字符串 + 派生字段；m_Script pathID 在字节偏移 20。
- 模型类：BrokenArrow.DataBase.Models.{Units,Weapons,Ammunitions,Turrets,Abilities,...}
  （SQLite 特性，24 表镜像）。

### 网格顶点流

- 通道索引语义（固定）：0=位置 1=法线 2=切线 4=UV0 5=UV1 12=蒙皮权重 13=骨骼索引。
- format：0=float32 1=float16 10=uint32 11=int32；内联数据每流按 16 字节对齐，
  流式(.resS)数据紧密排列。
- 流式网格：Mesh.m_StreamData = {path:"archive:/CAB-<hash>/CAB-<hash>.resS", offset, size}，
  实际文件在 `<bundle>_unpacked/CAB-<hash>.resS`。
- **骨骼名哈希（已破解 v1.8.0）**：`zlib.crc32(从 "root" 起的层级路径, '/' 连接, UTF-8)`；
  跳过 root_scale 节点；多索引槽位名取首个 _N（weapon_0_1_2→weapon_0，turret_0_0→turret_0，
  非纯数字尾缀不截断如 frontwing3_3_L）。例：crc32("root/body")=0x65A7524A、
  crc32("root/body/turret_0")=0x2A839EFE。工具用 bone_hashes.bone_hash() 直接计算，
  任意新骨骼名均可（不再依赖查找表）。
- 坐标系：Unity Y-up → Blender Z-up：位置 (x,y,z)→(x,-z,y)；四元数 (x,y,z,w)→(w,x,-z,y)。

### AnimationHub 行为（已破解 v1.8.0）

- MathConnect 布局：lod(i32) + ObjectFollower{freq,damper,reaction,defaultSerialized}(16B)
  + root PPtr + target PPtr + freeze 3 bool(4B) + **WeaponShotForces 字典**。
- **WeaponShotForces 字典布局**（实测样本约束求解）：
  `{i32 1, i32 0, i32 N, N×{i32 0, i64 TransformPathID}, i32 N, N×float}`。
- 全部 22 个行为类字段表已提取（AxisRandom/AxisRepeater/SpawnVFX/ShowVFX/AnimatorConnect/
  TurretFly/ShellCasingDrop 等，含嵌套类型），见 技术资料/extracts/behavior_layouts.json；
  工具侧未完全破译的类仍按字节透传（rawB64），往返无损。

### 载具皮肤（SkinStorageBridge，已破解 v1.8.2）

- 皮肤 = 材质替换：`SkinStorageBridge`（prefab 上的组件）存 `List<SkinStorageData>`，
  每条 = {Id + SkinDataDictionary{材质 PPtr → Renderer[]}}；游戏 SetSkin 把材质赋到对应渲染器。
- 桥字节布局（实测 322 个桥实例，往返无损）：MonoBehaviour 头 32B（含空 m_Name）后
  `i32 数量`；每条 `i32 Id` + `i32 keyCount` + keyCount×PPtr(材质 12B) + `i32 valCount` +
  valCount×{`i32 rCount` + rCount×PPtr(渲染器 12B)}。
- 桥都在炮塔/步兵 prefab 上（车体 prefab 无桥）；坦克炮塔 prefab 自带车体网格
  （如 RU_T90A 含 001_skinned_Chassis）；皮肤材质常跨 prefab 引用（同 bundle 内 pid 直连）。
- 皮肤材质为 HDRP：纹理槽名 `Layer_<hash>`（如 Layer_A97CDC25）；BaseMap 贴图名如
  `T90_1_BaseMap`。贴图多为流式（.resS），提取 PNG 需 `<bundle>_unpacked` 目录。
- 菜单皮肤列表在 data.unity3d 的 UserItemsConfig（SkinGroupDict/SkinPackDict；实例 pid 52173，
  220KB，无 typetree）——**重涂现有皮肤槽无需改它**；新增皮肤槽要在菜单显示需改该资产。
- 工具流：Blender ⑥ 皮肤（扫描/应用/导出贴图/打包 .bamod）→ BA_Mod_Maker 导入
  （新建 RGBA32 纹理 + 克隆材质换纹理槽 + 按皮肤 id 更新全部桥 + CRC）。
  .bamod manifest 新增 `skin:{target_id, new_id, mats:[{orig, texs:[{slot, tex}]}], textures:[{name, png}]}`。

### 步兵模型与姿势（已破解 v1.8.3）

- 步兵单位装配：Units.ModelFileName = 单位锚点（如 US_Rangers.prefab，仅 weapon_0_1/recoil/
  shell_spawn 等挂点，无网格）；SquadMembers.ModelFileName = **成员 = 完整士兵 prefab**。
- 士兵成员 prefab（如 Infantry/Motostrelki/RU_Motostrelki_rifle.prefab）preload 约 137 对象：
  48 GO/Transform（Hips→Spine→Spine1→Spine2→Neck→Head、左右肩/臂/前臂/手/五指、大腿/小腿/脚/脚趾 +
  WeaponLook/WeaponPlace/Heavy Weapons + lod_0/1/2）+ 3 蒙皮网格 + 3 纹理 + 1 材质 +
  Avatar + Animator + AnimatorController + **22 AnimationClip** + LODGroup + SkinStorageBridge。
- **姿势 = 烘焙在该 prefab 的骨骼本地变换里**（如持枪手 RightHand 的局部旋转）；改姿势 =
  改骨骼 Transform 局部旋转/位置，网格与蒙皮不变（bind pose 不变）。容器根 = prefab 同名 GO。
- 角色变体 = 同名系列（_rifle/_MG/_officer/_grenade/_Weapon/_heavy/_rifle1/_rifle_light），
  左右手姿势不同（RU 系左手持枪 + WeaponLook，US 系右手 + WeaponPlace）。
- 工具流：Blender ⑦ 步兵姿势（骨骼列表 + 欧拉滑条编辑 / 姿势库保存应用 / 从其他游戏模型
  按骨骼名复制姿势 / 重置到导入基线）→ copy-full 打包（pose-only，网格字节原样）→
  BA_Mod_Maker 导入；新模型路径留空=原地替换当前姿势，填新路径=新增姿势模型
  （需在数据库把 SquadMembers.ModelFileName 改成新模型名 + 注册地址）。

"""

# ---------------------------------------------------------------------------
# 地址映射与内部路径（原教程知识收纳）
# ---------------------------------------------------------------------------
ADDRESS_MAP = {
    "模型（数据表 ModelFileName 填的地址 ↔ bundle 内部路径，一一对应）": [
        ("车体/载具", "RU_BMPT", "Assets/Resources_moved/ModelPrefabs/RU/RU_BMPT.prefab"),
        ("炮塔", "RU_BMPT/RU_BMPT1", "Assets/Resources_moved/ModelPrefabs/RU/Turrets/RU_BMPT/RU_BMPT1.prefab"),
        ("新炮塔(例)", "RU_BMPT/RU_BMPT2_MOD", "Assets/Resources_moved/ModelPrefabs/RU/Turrets/RU_BMPT/RU_BMPT2_MOD.prefab"),
        ("飞机", "RU_AN72P", "Assets/Resources_moved/ModelPrefabs/Aircraft/RU_AN72P.prefab"),
        ("直升机", "RU_KA52", "Assets/Resources_moved/ModelPrefabs/Helicopters/RU_KA52.prefab"),
        ("机载武器/导弹", "AIM9_double", "Assets/Resources_moved/ModelPrefabs/Weapon/AIM9/AIM9_double.prefab"),
        ("步兵武器", "A545", "Assets/Resources_moved/ModelPrefabs/Infantry Weapons/RU/A545.prefab"),
        ("弹药模型", "100mm_RUS", "Assets/Resources_moved/Shared/Prefabs/Ammo/100mm_RUS.prefab"),
    ],
    "图标（地址 ↔ 内部路径）": [
        ("单位肖像", r"RU\BMPT_TERMINATOR\BMPT_TERMINATOR",
         "Assets/Resources_moved/Images/UnitPortraits/RU/BMPT_TERMINATOR/BMPT_TERMINATOR.png"),
        ("缩略图/标签", "RU_BMPT-Label", "Assets/Resources_moved/Images/Labels/Icons/RU_BMPT-Label.png"),
        ("武器图标", "2A72", "Assets/Resources_moved/Images/Weapons/Icons/2A72.png"),
        ("弹药图标", "AMMO_US_TANK_120_AP", "Assets/Resources_moved/Images/Ammunition/Icons/AMMO_US_TANK_120_AP.png"),
    ],
}
ADDRESS_RULES = """**命名规律**：
- 新模型的内部路径 = 原模型内部路径，换名加后缀（如 _MOD），保持唯一；
  数据表地址与内部路径同名（如 RU_BMPT → .../RU_BMPT.prefab；炮塔为 目录名/文件名 两段）。
- 前缀 RU = 俄罗斯 / US = 美国；DLC 单位在 DLC3/ 等子目录，照抄原路径所在目录。
- 改模型三步：① bundle 加 prefab（.bamod 导入）② catalog 注册地址
  ③ 数据表 ModelFileName/PortraitFileName/ThumbnailFileName 指向新地址。

"""

# ---------------------------------------------------------------------------
# 常量速查
# ---------------------------------------------------------------------------
CONSTANTS = [
    ("数据库魔数", "fhk3s0g3 (8B)", "加密字段前缀"),
    ("数据库 AES 密钥", "ASCII 09234237536700238099172758697347", "AES-256-CBC"),
    ("mod 对象 pid 段(units)", '0x4355424500000000("CUBE") ~ +0x10000', "工具新增对象专属，重导先清理"),
    ("mod 对象 pid 段(肖像)", '0x4355424600000000("CUBF")', "unitportraits bundle 新增对象"),
    ("UnitPrefabTurretInfo script pid", "6426374804064612000", "MonoBehaviour m_Script 指向"),
    ("AnimationHub script pid", "4665939560152279323", "军械库演示动画"),
    ("AnimationManagerBridge script pid", "8775279424834731323", "后坐/开火联动"),
    ("FmodTurretsTurn script pid", "-1118201132209727813", "炮塔转向音效"),
    ("units bundle", "aa/PC/units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle (3.66GB)", "模型主存放区"),
    ("炮塔骨骼高度", "body y=0.863；turret_0 在 body 上方 0.811（世界 y≈1.674）", "炮塔几何必须在 y≈1.674"),
    ("游戏主命名空间", "BrokenArrow（Steel Balalaika Studio）", "IL2CPP 类所在"),
]

# ---------------------------------------------------------------------------
# 防崩溃铁律与已知坑（原教程/说明/技术总结收纳）
# ---------------------------------------------------------------------------
RULES = """## 防崩溃铁律与已知坑

### 数据库五条铁律（每条都由真实崩溃验证）

1. db.zip 必须含重复的 `PlaneFlyPreset.json`（无 s）—— 否则 LoadMobility 空引用崩溃。
2. 每个单位必须有 UnitPropulsions 行（LoadMobility 同样）。
3. 每个单位武器必须有 WeaponAmmunitions 行 —— 否则武器不可见并崩溃单位信息卡。
4. 单位的 UnitArmors 中至少一条 `IsDefault=True` —— 否则"未指定装甲包"不生成。
5. 载具雷达/APS 必须放在 ECM 能力内（ECM=1.0 是"幽灵"载体）；APS 缺 Hitbox 字段不拦截。

### 模型/炮塔侧坑

- preload 顺序：MonoScript 必须排在 MonoBehaviour 之前 —— 否则战场报
  `Read 32 bytes but expected 64 bytes` / `script unknown or not yet loaded`（军械库不受影响）。
- LODGroup 必须字节复制（typetree 写会漏可选字段 → 畸形序列化 → Unity 断点崩溃）；
  没有 LODGroup 场景里不显示。
- AnimationManagerBridge 的 _recoils 字典键必须是真实蒙皮骨骼；空物体壳点会导致
  战场 BoneContainer.Bake 崩溃（当前构建不挂 Bridge）。
- 切线 W 必须 -1（否则模型倒置/法线贴图错误）；绕序用有符号体积自动翻转。
- 换现有模型外形只能移动顶点（增删顶点/改拓扑 → 权重动画失效）；直接构造的新模型拓扑任意。
- 改完 bundle 必须重算 CRC 并写回 catalog，否则报 CRC Mismatch。
- 累积构建残留旧对象/旧 preload 条目 → 战场崩溃（军械库正常，极具迷惑性）；
  build_final.py 必须从纯净备份构建，build_turret_direct 有幂等清理兜底。
- MathConnect 的 WeaponShotForces 字典未完全破译 → 按字节透传（shotsRawB64），
  面板往返不得重建为最小形式（会丢字节 → 反序列化越界）。
- 传感器覆盖：Options.MainSensorId 覆盖单位直连的 SensorUnits；游戏视野米数 = 数据库值 × 2。
- 新单位不能用全新 Id：占原生槽位（Miller/飞行员/训练靶）克隆；Country 只能 1(RUS)/2(USA)。

### 排查流程

1. GameLogs\\Gamelog__*.log 尾部（托管错误带完整类名堆栈）。
2. 错误模式对照：`Read 32 bytes but expected 64 bytes`=preload/脚本解析；`archive:/CAB-…
   is corrupted [Position out of bounds]`=对象序列化损坏；`CRC Mismatch`=catalog 未同步。
3. 军械库正常+战场崩 ⇒ LOD/BoneContainer.Bake/ECS 侧问题。

"""


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def field_type(rows, name):
    for r in rows:
        if isinstance(r, dict) and r.get(name) not in (None, ""):
            return type(r.get(name)).__name__
    return "unknown"


def zh_field(table, name):
    entry = BG.FIELDS.get(table, {}).get(name) or BG.COMMON.get(name)
    if entry:
        pair = entry.get("zh") or entry.get("en")
        if pair and isinstance(pair, (list, tuple)) and len(pair) >= 1:
            return (pair[0] or name, pair[1] if len(pair) > 1 else "")
    return (name, "")


def enum_lines():
    lines = ["## 枚举值对照", ""]
    for fname in sorted(BG.ENUMS):
        lines.append("### " + fname)
        lines.append("")
        lines.append("| 值 | 含义 |")
        lines.append("|---|---|")
        for item in BG.ENUMS[fname]:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            val = item[0]
            meaning = item[1] if len(item) > 1 else ""
            lines.append("| " + c(str(val)) + " | " + str(meaning) + " |")
        lines.append("")
    return lines


def address_lines():
    lines = ["## 地址映射与内部路径", ""]
    for title, rows in ADDRESS_MAP.items():
        lines.append("### " + title)
        lines.append("")
        lines.append("| 类型 | 地址（数据表填） | 内部路径（bundle 内） |")
        lines.append("|---|---|---|")
        for kind, addr, path in rows:
            lines.append("| " + kind + " | " + c(addr) + " | " + c(path) + " |")
        lines.append("")
    lines.append(ADDRESS_RULES)
    return lines


def constants_lines():
    lines = ["## 常量速查", ""]
    lines.append("| 常量 | 值 | 用途 |")
    lines.append("|---|---|---|")
    for name, value, use in CONSTANTS:
        lines.append("| " + name + " | " + c(value) + " | " + use + " |")
    lines.append("")
    return lines


def mounts_lines():
    lines = ["## 挂载点与组件词典", ""]
    lines.append("（数据源 blender_addon/mount_dict.py，全游戏 690 名扫描归纳（历史统计）。）")
    lines.append("")
    lines.append("### 挂载点分类")
    lines.append("")
    lines.append("| 分类 | 说明 |")
    lines.append("|---|---|")
    for cat in MOUNT_CATEGORIES:
        lines.append("| " + cat["name"] + " | " + cat["desc"] + " |")
    lines.append("")
    lines.append("高频名称：shell_spawn_0(1083)、weapon_0(964)、VFX_point(739)、root(676)、"
                "body(672)、turret_0(494)、death(198)、turret_1(174)、dust_point_left/right(169)、"
                "exhaust(148)、shell_spawn_0_0(131)、VFXPoint_OnSmokeAbility(127)、recoil_0_0(96)、"
                "weapon_0_1(91)（全游戏扫描频次）。")
    lines.append("")
    lines.append("### 组件（18 条）")
    lines.append("")
    lines.append("| 组件 | 类别 | 关键字段 |")
    lines.append("|---|---|---|")
    for nm, info in sorted(COMPONENTS.items()):
        fields = info.get("fields", "")
        lines.append("| " + nm + " | " + info.get("cat", "") + " | " + fields + " |")

    return lines


def build_md():
    md = ["# Broken Arrow 数据库词典 (Database Glossary)", ""]
    md.append("本文档由 clean_baseline.json（解密后的干净数据库）自动生成，")
    md.append("覆盖全部 24 张表、所有字段、外键关系与枚举值，")
    md.append("并收纳数据结构、地址映射、常量与防崩溃铁律（原教程/技术总结内容全部并入本文档）。")
    md.append("")

    md.append("## 数据总览")
    md.append("")
    md.append("| 表名 | 中文 | 行数 | 说明 |")
    md.append("|---|---|---:|---|")
    for t in TABLE_ORDER:
        if t not in BG.TABLES:
            continue
        zh, desc = BG.table_info(t, "zh")
        n = len(DATA.get(t, []))
        md.append("| " + c(t) + " | " + zh + " | " + str(n) + " | " + desc + " |")
    md.append("")

    # 每表字段
    for t in TABLE_ORDER:
        if t not in BG.TABLES or t not in DATA:
            continue
        zh, desc = BG.table_info(t, "zh")
        rows = DATA[t]
        md.append("## " + t + "（" + zh + "）")
        md.append("")
        md.append("> " + desc + " （行数：" + str(len(rows)) + "）")
        md.append("")
        md.append("| 字段 | 类型 | 含义 |")
        md.append("|---|---|---|")
        seen = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in seen:
                        seen.append(k)
        for name in seen:
            zhname, fdesc = zh_field(t, name)
            ft = field_type(rows, name)
            cell = zhname + ("：" + fdesc if fdesc else "")
            md.append("| " + c(name) + " | " + ft + " | " + cell + " |")
        md.append("")

    md += [DATA_STRUCTURES]
    md += enum_lines()
    md += address_lines()
    md += constants_lines()
    md += mounts_lines()
    md += [RULES]
    return "\n".join(md)


def main():
    text = build_md()
    out_md = os.path.join(HERE, "数据库词典.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)

    # JSON 版（zh）
    gloss = {"tables": {}}
    for t in TABLE_ORDER:
        if t not in BG.TABLES or t not in DATA:
            continue
        zh, desc = BG.table_info(t, "zh")
        rows = DATA[t]
        seen = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in seen:
                        seen.append(k)
        fields = {}
        for name in seen:
            zhname, fdesc = zh_field(t, name)
            fields[name] = {"zh": zhname, "desc": fdesc, "type": field_type(rows, name)}
        gloss["tables"][t] = {"zh": zh, "desc": desc, "row_count": len(rows), "fields": fields}
    gloss["enums"] = {
        k: [{"value": str(item[0]), "meaning": (item[1] if len(item) > 1 else "")}
            for item in BG.ENUMS[k] if isinstance(item, (list, tuple)) and item]
        for k in BG.ENUMS
    }
    gloss["constants"] = [{"name": n, "value": v, "use": u} for n, v, u in CONSTANTS]
    gloss["mount_categories"] = [{"id": x["id"], "name": x["name"], "desc": x["desc"]}
                                 for x in MOUNT_CATEGORIES]
    gloss["components"] = COMPONENTS
    with open(os.path.join(HERE, "database_glossary.json"), "w", encoding="utf-8") as f:
        json.dump(gloss, f, ensure_ascii=False, indent=2)

    print("OK 数据库词典.md chars:", len(text))
    print("tables:", len(gloss["tables"]), "enums:", len(gloss["enums"]))


if __name__ == "__main__":
    main()
