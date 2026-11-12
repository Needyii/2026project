# -*- coding: utf-8 -*-
"""岗位角色权限模型：Token 换角色、角色定数据边界，越界请求一律拦截。

数据红线3落地位置：本文件的 ROLE_SCOPES 是唯一权限真相源，
所有接口取数前必须经过 check_scope / visible_regions 校验，不许绕过。
"""
from backend.config_loader import CONFIG  # 读取 config.yaml 中的令牌映射表

# Token -> 角色ID 反查表：由 config.yaml 的 auth.tokens 自动生成，避免两处维护
TOKEN_TO_ROLE = {token: role for role, token in CONFIG["auth"]["tokens"].items()}  # 键值反转

# 各角色的数据边界定义（唯一真相源，接口层只许调用本表，不许自行放宽）：
#   name        —— 岗位显示名
#   region_type —— 可见区域类型集合；None 表示全部区域
#   regions     —— 可见区域ID精确列表；None 表示不按ID限定
#   features    —— 可用功能集合：overview=客流总览 / alerts=预警 / history=历史趋势 /
#                  suggestions=调度建议 / confirm=确认调度 / ticket=票务数据
ROLE_SCOPES = {
    "yunying_zhuren": {          # —— 运营主任（顾明岚）：全园全量 + 审批
        "name": "运营主任",        # 岗位名
        "region_type": None,      # None = 不限类型，全园可见
        "regions": None,          # None = 不按ID限定
        "features": {"overview", "alerts", "history", "suggestions", "confirm", "ticket"},  # 全功能
    },
    "rukou_yindao": {            # —— 入口引导员（林亦舟）：只看入口，无后台权限
        "name": "入口引导员",      # 岗位名
        "region_type": {"entrance"},  # 只可见 entrance 类型区域
        "regions": None,          # 不按ID额外限定
        "features": {"overview", "alerts", "instructions"},  # 无 suggestions/confirm/ticket/history
    },
    "diaodu_yuan": {             # —— 游客服务调度员（唐映秋）：全域密度 + 空区
        "name": "游客服务调度员",  # 岗位名
        "region_type": None,      # 全园可见（负责全域调配）
        "regions": None,          # 不按ID限定
        "features": {"overview", "alerts", "suggestions"},  # 可接收建议但确认权在运营主任
    },
    "piaowu_gongchengshi": {     # —— 票务数据工程师（秦芮）：票务/入园数据，不参与现场调度
        "name": "票务数据工程师",  # 岗位名
        "region_type": None,      # 区域类型不限定（校准工作需要对照全园计数）
        "regions": None,          # 不按ID限定
        "features": {"ticket", "history"},  # 只有票务统计与历史趋势，无调度建议功能
    },
    "anquan_fuzeren": {          # —— 安全负责人（傅景）：风险区 + 预警
        "name": "安全负责人",      # 岗位名
        "region_type": None,      # 风险管控需要全园视野
        "regions": None,          # 不按ID限定
        "features": {"overview", "alerts"},  # 无建议生成与确认权限（安全动作必须人工指挥，红线）
    },
    "shanghu_daibiao": {         # —— 商户服务代表（叶书）：仅片区趋势，无全局敏感数据
        "name": "商户服务代表",    # 岗位名
        "region_type": {"commercial"},  # 只可见 commercial 商户片区
        "regions": None,          # 不按ID限定
        "features": {"overview", "history"},  # 看趋势做经营预判，拿不到入口/安全数据
    },
}

def resolve_role(token: str) -> str | None:  # 令牌解析函数
    """把访问令牌解析为角色ID。

    入参：token —— 调用方携带的访问令牌字符串。
    出参：角色ID字符串；令牌无效返回 None。
    失败行为：不抛异常，返回 None 由调用方统一返回 401 结构体。
    """
    return TOKEN_TO_ROLE.get(token)  # 查表返回；查不到即 None

def check_feature(role: str, feature: str) -> bool:  # 功能权限校验
    """校验角色是否拥有某项功能权限。

    入参：role —— 角色ID；feature —— 功能名（见 ROLE_SCOPES.features 注释）。
    出参：bool —— True 允许 / False 拦截。
    失败行为：未知角色一律返回 False（默认拒绝原则：不在名单内=无权限）。
    """
    scope = ROLE_SCOPES.get(role)  # 取角色权限定义
    if scope is None:              # 未知角色（理论上 resolve_role 已拦截，双保险）
        return False               # 默认拒绝，防止未定义角色穿透
    return feature in scope["features"]  # 按功能集合判断

def visible_regions(role: str, regions: list[dict]) -> list[dict]:  # 数据行级过滤
    """按角色权限过滤区域列表，返回该角色可见的子集。

    入参：role —— 角色ID；regions —— 全量区域字典列表（含 type 字段）。
    出参：过滤后的区域列表（同一批 dict 对象，字段已在调用方裁剪）。
    失败行为：未知角色返回空列表（不给任何数据，而不是报错泄露存在性）。
    """
    scope = ROLE_SCOPES.get(role)  # 取角色权限定义
    if scope is None:              # 未知角色
        return []                  # 返回空，绝不泄露任何区域信息
    if scope["region_type"] is None:  # 类型不限定的角色（如运营主任）
        return list(regions)       # 直接返回全量副本
    # 类型限定角色（引导员/商户）：只保留其可见的区域类型
    return [r for r in regions if r["type"] in scope["region_type"]]  # 列表推导过滤
