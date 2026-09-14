# -*- coding: utf-8 -*-
r"""挂载点词典 —— 断箭模型挂载点分类、命名规则与用途说明。

数据来源：对 units_assets_all.bundle 全部 prefab 层级的实测扫描
（_rev_tools/mountpoints_scan.json，690 个名称、每个名称的频次/父级/组件），
结合对 TI（UnitPrefabTurretInfo）、AnimationHub、LODGroup 等组件的逆向分析。

用法：
    from mount_dict import MOUNT_CATEGORIES, MOUNT_POINTS, suggest_parents
    MOUNT_POINTS["turret_0"]  -> {"cat": "turret", "desc": "...", "parents": [...], "note": "..."}
"""

# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
MOUNT_CATEGORIES = [
    {"id": "turret",   "name": "炮塔/旋转座", "icon": "PIVOT_CURSOR",
     "desc": "可旋转的炮塔座圈骨骼。turret_N 由 UnitPrefabTurretInfo 驱动旋转（战场 ECS），"
             "军械库里由 AnimationHub 演示行为扫掠。"},
    {"id": "weapon",   "name": "武器挂点", "icon": "CROSSHAIR",
     "desc": "武器俯仰挂点。weapon_0_1_2 = 炮塔0第1武器第2身管；weapon_0_1 = 炮塔0第1武器；"
             "weapon_0 = 单武器。TI 里每个 WeaponPointData 对 = weapon(俯仰) + shell_spawn(抛壳)。"},
    {"id": "shell",    "name": "弹药/抛壳点", "icon": "ORIENTATION_GLOBAL",
     "desc": "开火抛壳/出膛位置。shell_spawn_X_Y = 第X武器第Y抛壳口；带 (N) 后缀 = 多管/弹舱逐个口。"
             "AnimationManagerBridge 的后坐字典键必须指向真实骨骼。"},
    {"id": "recoil",   "name": "后坐骨", "icon": "FORCE_DRAG",
     "desc": "开火后坐动画骨骼（recoil_0_0 = 武器0后坐骨0）。由 AnimationManagerBridge/AnimationManager 驱动。"},
    {"id": "sensor",   "name": "观瞄/传感器", "icon": "CON_CAMERASOLVER",
     "desc": "观瞄镜、瞄准具、雷达、天线。scope/scope_0/scope_01、SpecialScope、radar、antenna 等；"
             "天线类常被 AnimationHub 的 AxisRandom/MathConnect 引用做演示晃动。"},
    {"id": "vfx",      "name": "特效点", "icon": "LIGHT",
     "desc": "VFX 位置：开火口焰 fire_y/fire_z、炮塔飞离 turret_fly_VFX、烟幕 VFXPoint_OnSmokeAbility、"
             "干扰弹 VFXPoint_OnCMAbility、加力 Afterburner、废烟 exhaust、扬尘 dust_point_*、"
             "死亡 death、通用 VFX_point/VFX_N。"},
    {"id": "helper",   "name": "辅助/约束", "icon": "CONSTRAINT",
     "desc": "helper/aim/Target/anim_*：瞄准辅助、IK 目标、约束参照、动画容器（anim_turret/anim_turret_0），"
             "通常带 PositionConstraint/RotationConstraint/LookAtConstraint。"},
    {"id": "door",     "name": "舱门/座位", "icon": "COMMUNITY",
     "desc": "载员舱门与座位：Entrances/Door/Door (N)、Seats/Seat (N)，供进出舱逻辑挂载。"},
    {"id": "track",    "name": "轮系/履带", "icon": "ANIM",
     "desc": "履带/负重轮骨骼：Track_L/Track_R、LR000-LR010、RR000-RR010、L001-L006/R001-R006，"
             "由悬挂/行走动画系统驱动。"},
    {"id": "fly",      "name": "飞行/旋翼", "icon": "FORCE_TURBULENCE",
     "desc": "飞行器部件：旋翼 Rotorangle_0/N、机翼 wing_*、发动机 Engine_*、喷口、关节 joint_*。"},
    {"id": "structure","name": "结构/骨骼", "icon": "BONE_DATA",
     "desc": "基础结构：root（prefab 根）、body（根骨骼，蒙皮根的父级）、BMPT1 式内部根。"},
]

# ---------------------------------------------------------------------------
# 挂载点条目（name -> 信息）
# ---------------------------------------------------------------------------
MOUNT_POINTS = {
    # ---- 结构 ----
    "root": {"cat": "structure", "desc": "prefab 根节点（原点）。所有骨骼/挂载点的最终祖先，组件挂在这里（LODGroup/Hub/Bridge）。",
             "parents": ["无"], "note": "构建时自动生成，一般不需要手动创建"},
    "body": {"cat": "structure", "desc": "根骨骼。蒙皮网格的 m_RootBone，绝大多数骨骼的父级，y≈0.863（相对 root）。",
             "parents": ["root"], "note": "绑定姿势=平移(0,-0.863,0)的逆"},
    # ---- 炮塔 ----
    "turret_0": {"cat": "turret", "desc": "主炮塔旋转座（TI 的 turret_index=0）。挂 UnitPrefabTurretInfo，"
                 "战场 ECS 驱动旋转，军械库演示动画扫掠 -45°~+45°。", "parents": ["body"],
                 "note": "TI 的 m_GameObject 指向它；必须有"},
    "turret_1": {"cat": "turret", "desc": "第 2 个炮塔/武器站（机枪塔、导弹塔等）。", "parents": ["body", "turret_0"]},
    "turret_2": {"cat": "turret", "desc": "第 3 个炮塔/武器站。", "parents": ["body", "turret_1"]},
    "turret_3": {"cat": "turret", "desc": "第 4 个炮塔（直升机/无人机常见多挂架）。", "parents": ["body"]},
    "turret_0_0": {"cat": "turret", "desc": "炮塔0的子转塔（同轴/独立旋转的部件，如 T90 的遥控机枪座）。", "parents": ["turret_0"]},
    "turret": {"cat": "turret", "desc": "无编号炮塔（底盘本身的炮塔座，T80U 等用）。", "parents": ["body", "Chassis"]},
    "turret_anim_container": {"cat": "helper", "desc": "炮塔动画容器（炮塔动画层级挂在这里）。", "parents": ["body"]},
    # ---- 武器 ----
    "weapon_0": {"cat": "weapon", "desc": "炮塔0的主武器俯仰点（单管）。TI 的 weapon 引用。", "parents": ["turret_0"]},
    "weapon_1": {"cat": "weapon", "desc": "炮塔0的第 2 武器俯仰点（并列机枪等）。", "parents": ["turret_0", "turret_1"]},
    "weapon_0_1": {"cat": "weapon", "desc": "炮塔0第1武器的俯仰点（命名规律：weapon_炮塔_武器）。", "parents": ["turret_0"]},
    "weapon_0_1_2": {"cat": "weapon", "desc": "炮塔0第1武器第2身管的俯仰点（weapon_炮塔_武器_身管，BMP2 式双联装）。",
                     "parents": ["turret_0"], "note": "多身管时每个身管一个"},
    # ---- 弹药/抛壳 ----
    "shell_spawn_0": {"cat": "shell", "desc": "武器0的抛壳/出膛点。TI 的 shellSpawn 引用。",
                      "parents": ["weapon_0", "weapon_0_1", "weapon_0_1_2"]},
    "shell_spawn_0_0": {"cat": "shell", "desc": "炮塔0武器0的抛壳点0（命名规律：shell_spawn_武器_口）。",
                        "parents": ["weapon_0_1", "weapon_0_1_2"]},
    "shell_spawn_1_0": {"cat": "shell", "desc": "炮塔0武器1的抛壳点0。", "parents": ["weapon_0_1", "weapon_0_1_2"]},
    "shell_spawn_2_0": {"cat": "shell", "desc": "炮塔0武器2的抛壳点0。", "parents": ["weapon_0_1_2"]},
    # ---- 后坐 ----
    "recoil_0_0": {"cat": "recoil", "desc": "武器0的后坐骨0（recoil_武器_骨）。开火后坐动画驱动它。",
                   "parents": ["weapon_0_1", "weapon_0_1_2", "weapon_0"]},
    "recoil_0": {"cat": "recoil", "desc": "武器0的后坐骨（无身管编号）。", "parents": ["weapon_0"]},
    "recoil_1_0": {"cat": "recoil", "desc": "武器1的后坐骨0。", "parents": ["weapon_0_1_2", "weapon_0_1"]},
    # ---- 观瞄/电子 ----
    "scope": {"cat": "sensor", "desc": "观瞄镜（炮长镜）。演示动画 MathConnect 可能引用。", "parents": ["turret_0", "body"]},
    "scope_0": {"cat": "sensor", "desc": "观瞄镜0（多层观瞄 scope_0 → scope_00/scope_01 子级）。", "parents": ["turret_0"]},
    "scope_00": {"cat": "sensor", "desc": "观瞄镜0的内层0。", "parents": ["scope_0"]},
    "scope_01": {"cat": "sensor", "desc": "观瞄镜0的内层1。", "parents": ["scope_0"]},
    "SpecialScope": {"cat": "sensor", "desc": "特殊观瞄（BMP2 式反坦克导弹瞄准具），演示 AxisRandom 引用。", "parents": ["turret_0"]},
    "radar": {"cat": "sensor", "desc": "雷达。", "parents": ["turret_0", "body"]},
    "antenna": {"cat": "sensor", "desc": "天线（无编号）。演示 AxisRandom 常引用做随机晃动。", "parents": ["turret_0", "body"]},
    "antenna_0": {"cat": "sensor", "desc": "天线0。", "parents": ["turret_0", "body"]},
    "antenna_00": {"cat": "sensor", "desc": "天线00（多根天线编号）。", "parents": ["turret_0"]},
    "antenna_01": {"cat": "sensor", "desc": "天线01。", "parents": ["turret_0"]},
    "Lantenna_02": {"cat": "sensor", "desc": "左天线02（BMP2M 式），演示 MathConnect 的目标。", "parents": ["turret_0"]},
    "Rantenna_02": {"cat": "sensor", "desc": "右天线02，演示 MathConnect 的目标。", "parents": ["turret_0"]},
    # ---- 特效 ----
    "fire_y": {"cat": "vfx", "desc": "炮口火焰特效点（横向）。", "parents": ["weapon_0_1", "root"]},
    "fire_z": {"cat": "vfx", "desc": "炮口火焰特效点（纵向）。", "parents": ["weapon_0_1", "root"]},
    "turret_fly_VFX": {"cat": "vfx", "desc": "炮塔飞离特效点（殉爆时炮塔飞出的位置）。", "parents": ["turret_0"]},
    "VFXPoint_OnSmokeAbility": {"cat": "vfx", "desc": "烟幕能力特效点。", "parents": ["body"]},
    "VFXPoint_OnCMAbility": {"cat": "vfx", "desc": "干扰弹/反制能力特效点。", "parents": ["body"]},
    "VFX_point": {"cat": "vfx", "desc": "通用特效点（航弹挂点等，带 (N) 后缀 = 多枚）。", "parents": ["武器挂架"]},
    "VFX_1": {"cat": "vfx", "desc": "通用特效点1。", "parents": ["body"]},
    "VFX_2": {"cat": "vfx", "desc": "通用特效点2。", "parents": ["body"]},
    "Afterburner": {"cat": "vfx", "desc": "加力燃烧室特效点（喷气机）。", "parents": ["body"]},
    "VFX_AfterBurner_Inversion_L": {"cat": "vfx", "desc": "左发反推特效点。", "parents": ["body"]},
    "VFX_AfterBurner_Inversion_R": {"cat": "vfx", "desc": "右发反推特效点。", "parents": ["body"]},
    "VFX_inversion_trace_L": {"cat": "vfx", "desc": "左翼尖涡流特效点。", "parents": ["body", "机翼"]},
    "VFX_inversion_trace_R": {"cat": "vfx", "desc": "右翼尖涡流特效点。", "parents": ["body", "机翼"]},
    "exhaust": {"cat": "vfx", "desc": "发动机废气点。", "parents": ["body", "root"]},
    "exhaust_left": {"cat": "vfx", "desc": "左侧废气点。", "parents": ["body"]},
    "exhaust_right": {"cat": "vfx", "desc": "右侧废气点。", "parents": ["body"]},
    "dust_point_left": {"cat": "vfx", "desc": "左履带扬尘点。", "parents": ["body"]},
    "dust_point_right": {"cat": "vfx", "desc": "右履带扬尘点。", "parents": ["body"]},
    "death": {"cat": "vfx", "desc": "死亡特效基准点（殉爆/残骸位置）。", "parents": ["body", "root"]},
    # ---- 辅助 ----
    "helper": {"cat": "helper", "desc": "通用辅助点（瞄准/约束参照）。", "parents": ["turret_0", "weapon_0"]},
    "helper_01": {"cat": "helper", "desc": "辅助点01。", "parents": ["turret_0"]},
    "helper_constrain": {"cat": "helper", "desc": "约束辅助点（限制瞄准范围的参照）。", "parents": ["turret_0"]},
    "anim_turret": {"cat": "helper", "desc": "炮塔动画容器/参照（演示动画目标）。", "parents": ["body"]},
    "anim_turret_0": {"cat": "helper", "desc": "炮塔0动画参照。", "parents": ["body"]},
    "anim_weapon_0": {"cat": "helper", "desc": "武器0动画参照。", "parents": ["turret_0"]},
    # ---- 舱门/座位 ----
    "Entrances": {"cat": "door", "desc": "载员进出口容器。", "parents": ["body"]},
    "Door": {"cat": "door", "desc": "舱门。", "parents": ["Entrances", "body"]},
    "hatch": {"cat": "door", "desc": "舱盖。", "parents": ["body", "turret_0"]},
    "Seats": {"cat": "door", "desc": "座位容器。", "parents": ["body"]},
    "Seat": {"cat": "door", "desc": "座位。", "parents": ["Seats"]},
    # ---- 轮履 ----
    "Track_L": {"cat": "track", "desc": "左履带/轮组。", "parents": ["Chassis"]},
    "Track_R": {"cat": "track", "desc": "右履带/轮组。", "parents": ["Chassis"]},
    "LR000": {"cat": "track", "desc": "左负重轮0（LR = Left Roadwheel）。", "parents": ["body"]},
    "RR000": {"cat": "track", "desc": "右负重轮0。", "parents": ["body"]},
    # ---- 飞行 ----
    "Rotorangle_0": {"cat": "fly", "desc": "主旋翼轴。", "parents": ["body"]},
    "Rotorangle_1": {"cat": "fly", "desc": "尾旋翼/第二旋翼轴。", "parents": ["body"]},
    "engine1_L": {"cat": "fly", "desc": "左发1（发动机吊舱）。", "parents": ["机翼"]},
    "backwing_L": {"cat": "fly", "desc": "左后翼。", "parents": ["body"]},
    "backwing_R": {"cat": "fly", "desc": "右后翼。", "parents": ["body"]},
    "wing_L_1": {"cat": "fly", "desc": "左翼段1。", "parents": ["body"]},
    "wing_R_1": {"cat": "fly", "desc": "右翼段1。", "parents": ["body"]},
}

# 关键词 -> 类别（供自动归类未收录的名称）
KEYWORD_CAT = {
    "turret": "turret", "weapon": "weapon", "shell_spawn": "shell", "recoil": "recoil",
    "scope": "sensor", "optic": "sensor", "sight": "sensor", "radar": "sensor",
    "antenna": "sensor", "antena": "sensor",
    "vfx": "vfx", "fire": "vfx", "smoke": "vfx", "dust": "vfx", "exhaust": "vfx",
    "afterburner": "vfx", "death": "vfx", "chaff": "vfx", "flare": "vfx",
    "helper": "helper", "aim": "helper", "target": "helper", "constrain": "helper", "anim_": "helper",
    "door": "door", "hatch": "door", "seat": "door", "entrance": "door",
    "lr": "track", "rr": "track", "track": "track", "wheel": "track", "roadwheel": "track",
    "rotor": "fly", "wing": "fly", "engine": "fly", "joint": "fly", "blade": "fly", "propeller": "fly",
    "body": "structure", "root": "structure",
}


def category_of(name):
    """按词典/关键词给挂载点分类，返回分类 id。"""
    if name in MOUNT_POINTS:
        return MOUNT_POINTS[name]["cat"]
    low = name.lower()
    for k, c in KEYWORD_CAT.items():
        if k in low:
            return c
    return "helper"


# ---------------------------------------------------------------------------
# 组件结构（IL2CPP MonoBehaviour 字段；换/加挂点时改的是这些 PPtr 引用）
# ---------------------------------------------------------------------------
COMPONENTS = {
    "UnitPrefabTurretInfo": {
        "cat": "turret",
        "desc": "炮塔信息——旋转 + 开火核心，挂在 turret_N 上。",
        "fields": "TurretIndex(int32 炮塔下标，0=主炮塔) · Weapons(list 每项24B：{WeaponTransform(PPtr)→weapon_N 俯仰点, ShellSpawn(PPtr)→shell_spawn_N 开火点})",
    },
    "WeaponPrefabInfo": {
        "cat": "weapon",
        "desc": "武器预制体自身（导弹/火箭发射筒）。",
        "fields": "ShellSpawns(list 每项：{ShellSpawn(PPtr)→发射口, AmmunitionObject(PPtr)→弹药模型(可0), EmptyLauncherCover(PPtr)→空发射筒盖(可0)})",
    },
    "EffectsSpawnPoint": {
        "cat": "vfx",
        "desc": "特效挂点。",
        "fields": "VFXPrefabRef(PPtr 特效预制体) · LifeTime(float) · AttachVfxToObject(bool) · PlayForce(bool) · SoundEventString(str 音效事件名)",
    },
    "AnimationManager": {
        "cat": "helper",
        "desc": "车体/武器动画桥（后坐字典键须指向真实骨骼）。",
        "fields": "_root/_body/_wheels/_suspension/_traks(车体动画) · _recoils(dict 武器→recoil_N 后坐挂点) · _weaponsVfxContainer(dict 武器→炮口特效)",
    },
    "UnitPrefabRoot": {
        "cat": "structure",
        "desc": "单位根标记。",
        "fields": "PrefabRoot / PrefabBody / SpawnTimeObjects / Renderers / Skins.Data",
    },
    "ContainerSeatInitializer": {"cat": "door", "desc": "运输载具座位。", "fields": "UnitSeatAnimation → 座位动画"},
    "InfantryHandWeapon": {"cat": "weapon", "desc": "步兵手持武器。", "fields": "ShellSpawnPosition → 枪口"},
    "PlaneInitializer": {"cat": "fly", "desc": "固定翼飞机。", "fields": "PilotID → 飞行员"},
    "FmodTurretsTurn": {"cat": "turret", "desc": "炮塔转向音效。", "fields": "EventName / TargetBone"},
    "FastIKFabric": {"cat": "helper", "desc": "士兵脚部 IK。", "fields": "—"},
    "SkinStorageBridge": {"cat": "helper", "desc": "换肤桥。", "fields": "—"},
    "DecalProjector": {"cat": "vfx", "desc": "贴花投影。", "fields": "—"},
    "DriverMarker": {"cat": "helper", "desc": "驾驶标记（DriverBehaviour / DriverController）。", "fields": "—"},
    "DestroyInBattleMarker": {"cat": "helper", "desc": "战斗销毁标记。", "fields": "—"},
    "UnitPersistentObjectMarker": {"cat": "helper", "desc": "持久对象标记。", "fields": "—"},
    "MaterialQualityManager": {"cat": "helper", "desc": "材质质量。", "fields": "—"},
    "PhysFly": {"cat": "fly", "desc": "飞行物理。", "fields": "—"},
    "SoldierAnimationManager": {"cat": "helper", "desc": "士兵动画。", "fields": "—"},
}

