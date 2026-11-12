# -*- coding: utf-8 -*-
"""调度建议引擎：基于规则从客流现状生成可落地的分流建议。

设计原则（对齐需求发现报告 REQ-1/REQ-3）：
  1. 规则兜底先行——拥堵/空区配对是确定性逻辑，不依赖模型；
  2. 安全红线硬编码——只输出"引导/解释/排班"类建议，
     绝不生成限流、疏散、闸机控制类自动执行动作；
  3. 所有建议必须人工确认后才算调度动作（确认接口在 main.py）。
"""
import uuid  # 为每条建议生成唯一编号，便于留痕追溯
from backend.config_loader import CONFIG  # 读取拥堵/空区阈值

# 安全红线（需求发现报告 f41/f42/f52，不可逆风险边界）：
# 本引擎输出的建议全部为"人员引导类"动作，只读呈现给岗位人员，
# 不触碰闸机、广播、限流、疏散等任何自动执行通道，
# 真正的调度动作必须由人工确认（dispatch confirm 接口）并留痕。
_FORBIDDEN_ACTIONS = {"限流", "疏散", "关闭闸机", "强制改道", "广播疏散"}  # 黑名单：规则引擎禁止输出的动作词

def _status(ratio: float, congestion: float, idle: float) -> str:  # 区域状态判定
    """根据占容量比判定区域状态。

    入参：ratio —— 占容量比（0~1+）；congestion —— 拥堵阈值；idle —— 空区阈值。
    出参："congested" 拥堵 / "idle" 空区 / "normal" 正常。
    失败行为：负数比例按 0 处理，不抛异常。
    """
    ratio = max(0.0, ratio)  # 兜底负数
    if ratio >= congestion:  # 越过拥堵线
        return "congested"   # 拥堵
    if ratio <= idle:        # 低于空区线
        return "idle"        # 空区
    return "normal"          # 其余为正常

def build_suggestions(regions: list[dict]) -> list[dict]:  # 建议生成主函数
    """从全园区域现状生成"入口拥堵 → 空区"的配对分流建议。

    入参：regions —— 区域现状列表（含 current_count/capacity/type 等字段）。
    出参：list[dict] —— 建议列表，每条含 id/action/target_region/detail/requires_human/safety_note。
    失败行为：入参为空或全部正常时返回空列表，不抛异常。
    """
    sim_cfg = CONFIG["simulation"]  # 取模拟配置段
    congestion = sim_cfg["congestion_ratio"]  # 拥堵阈值
    idle = sim_cfg["idle_ratio"]  # 空区阈值
    # 先给每个区域算比率与状态，一次遍历，两处使用
    enriched = []  # 富化后的区域列表
    for r in regions:  # 逐区域计算
        capacity = r["capacity"] or 1  # 容量兜底为1，防除零
        ratio = r["current_count"] / capacity  # 占容量比
        enriched.append({**r, "ratio": round(ratio, 4),  # 保留4位小数便于前端展示
                         "status": _status(ratio, congestion, idle)})  # 状态判定
    entrances = [r for r in enriched if r["status"] == "congested" and r["type"] == "entrance"]  # 拥堵入口
    idle_gardens = [r for r in enriched if r["status"] == "idle" and r["type"] == "garden"]  # 空闲园区
    suggestions = []  # 结果列表
    # 配对逻辑：拥堵入口 × 空闲园区，生成"引导分流"建议（对齐 REQ-1 可见性→分流链路）
    for e in entrances:  # 遍历拥堵入口
        if not idle_gardens:  # 没有空区可分流
            # 仍然给建议：仅提示拥堵现状，由调度员人工判断去向
            sug = {  # 单条建议
                "id": uuid.uuid4().hex[:12],  # 建议唯一编号（留痕追溯用）
                "region_id": e["id"], "region_name": e["name"],  # 拥堵区域
                "action": "拥堵提示",  # 动作名
                "target_region": None,  # 无明确去向
                "detail": f"{e['name']}当前占比{e['ratio']:.0%}已超拥堵阈值，请调度员人工评估疏导方向",  # 给人看的说明
                "requires_human": True,  # 必须人工确认
                "safety_note": "仅提示，不自动实施任何限流或疏散动作",  # 红线声明
            }  # 组装完成
            suggestions.append(sug)  # 入列
            continue  # 进入下一个入口
        for g in idle_gardens:  # 有空区：生成配对建议
            sug = {  # 单条建议
                "id": uuid.uuid4().hex[:12],  # 建议唯一编号
                "region_id": e["id"], "region_name": e["name"],  # 拥堵区域
                "action": "引导分流",  # 动作名
                "target_region": g["id"],  # 建议去向：空闲园区
                "detail": (f"{e['name']}占比{e['ratio']:.0%}拥堵，{g['name']}占比{g['ratio']:.0%}空置，"  # 数据依据
                           f"建议引导员向游客指引{g['name']}方向"),  # 可落地动作
                "requires_human": True,  # 必须人工确认后才下发指令
                "safety_note": "仅引导类建议，不自动实施限流/疏散，不控制闸机",  # 红线声明
            }  # 组装完成
            # 双保险：硬校验建议文本，防止未来维护时误把禁止动作写进模板
            assert not any(w in sug["action"] + sug["detail"] for w in _FORBIDDEN_ACTIONS), \
                f"建议包含禁止动作词: {sug['id']}"  # 触发即配置错误，直接崩溃修复
            suggestions.append(sug)  # 入列
    # 安全侧建议：高风险区域提示安全负责人关注（只读提示，无执行动作）
    for r in enriched:  # 遍历全部区域
        if r["type"] != "entrance" and r["status"] == "congested":  # 园内拥堵（非入口）
            suggestions.append({  # 安全提示类建议
                "id": uuid.uuid4().hex[:12],  # 唯一编号
                "region_id": r["id"], "region_name": r["name"],  # 区域
                "action": "安全关注",  # 动作名
                "target_region": None,  # 无去向
                "detail": f"{r['name']}占比{r['ratio']:.0%}异常升高，请安全负责人现场巡查确认",  # 说明
                "requires_human": True,  # 人工判断
                "safety_note": "触及停止条件时由安全负责人指挥处置，系统不自动动作",  # 对齐 f42
            })  # 组装完成
    return suggestions  # 返回建议列表
