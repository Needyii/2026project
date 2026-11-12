# -*- coding: utf-8 -*-
"""FastAPI 主应用：统一返回结构、Token 鉴权、角色过滤接口、WebSocket 实时推送。

对外接口统一结构（交付规范）：
    {"ok": bool, "data": ..., "error": {"code": str, "msg": str}}
"""
from contextlib import asynccontextmanager  # lifespan 上下文管理器
from datetime import datetime               # 留痕时间戳
from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect  # 框架组件
from fastapi.middleware.cors import CORSMiddleware  # 跨域中间件（阶段2前端联调用）
from fastapi.responses import JSONResponse  # 自定义异常响应体
from backend.config_loader import CONFIG    # 全局配置
from backend import db, simulator           # 数据库层与模拟器
from backend.roles import (resolve_role, check_feature, visible_regions, ROLE_SCOPES)  # 权限模型
from backend.scheduler_agent import build_suggestions  # 调度建议引擎

# ---------- 统一返回结构工具 ----------
def ok(data) -> dict:  # 成功响应构造
    """构造统一成功响应体。入参 data=业务数据；出参={"ok":True,"data":data,"error":None}；无失败分支。"""
    return {"ok": True, "data": data, "error": None}  # 固定三键结构

def fail(code: str, msg: str, status: int = 200) -> JSONResponse:  # 失败响应构造
    """构造统一失败响应体。入参 code=错误码, msg=中文说明, status=HTTP状态码；返回 JSONResponse；无失败分支。"""
    return JSONResponse(status_code=status,  # HTTP 层仍给出语义状态码
                        content={"ok": False, "data": None, "error": {"code": code, "msg": msg}})  # 业务层统一结构

# ---------- 生命周期：启动/停止模拟器 ----------
@asynccontextmanager  # 标记为 lifespan 异步上下文
async def lifespan(app: FastAPI):  # 应用生命周期钩子
    """应用启动时开启客流模拟线程，关闭时优雅停止。入参 app=应用实例；出参：无；失败则启动中止。"""
    simulator.start()  # 启动后台模拟线程
    yield              # 服务运行中（挂起，直到关闭）
    simulator.stop()   # 关闭时停止线程，保证优雅退出

app = FastAPI(title="青麓景区客流调度智能体", version="0.1.0", lifespan=lifespan)  # 创建应用实例

# 跨域中间件：阶段2本地前端（Vite 默认5173端口）需要跨域访问后端
app.add_middleware(CORSMiddleware, allow_origins=["*"],  # 演示阶段放开；上线前收敛为前端域名白名单
                   allow_methods=["*"], allow_headers=["*"])  # 方法与头全放行

# ---------- 鉴权依赖 ----------
def get_role(request: Request) -> str:  # 鉴权依赖函数
    """从 Authorization 头解析 Bearer Token 并映射为角色ID。

    入参：request —— FastAPI 请求对象。
    出参：角色ID字符串。
    失败行为：缺少头/令牌无效时抛 HTTPException(401)，由统一异常处理器包装为规范结构体。
    """
    auth = request.headers.get("Authorization", "")  # 读取鉴权头
    if not auth.startswith("Bearer "):  # 必须是 Bearer 格式
        raise HTTPException(status_code=401, detail="缺少有效的 Authorization Bearer 令牌")  # 401
    token = auth.removeprefix("Bearer ").strip()  # 剥离前缀取纯令牌
    role = resolve_role(token)  # 令牌换角色
    if role is None:  # 令牌不在配置表中
        # 为什么这么卡：无效令牌一律拒绝且不区分"错在哪个环节"，避免给攻击者探测信息
        raise HTTPException(status_code=401, detail="令牌无效或已失效")  # 统一401
    return role  # 返回角色ID

@app.exception_handler(HTTPException)  # 注册 HTTP 异常统一处理器
async def http_exc_handler(request: Request, exc: HTTPException):  # 处理函数
    """把框架抛出的 HTTPException 包装为统一 {"ok","data","error"} 结构。

    入参：request —— 请求对象；exc —— 异常实例。
    出参：JSONResponse —— 统一结构体。
    失败行为：本身即失败处理器，无再失败分支。
    """
    code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(exc.status_code, "ERROR")  # 状态码→错误码映射
    return fail(code, exc.detail, exc.status_code)  # 统一包装返回

# ---------- 通用工具 ----------
def load_regions() -> list[dict]:  # 读取全园现状
    """从 region_flow 表读取全部区域现状行，转为字典列表。

    入参：无。
    出参：list[dict] —— 每个区域含 id/name/type/capacity/current_count/updated_at。
    失败行为：表为空时返回空列表（模拟器未启动/尚未写入第一轮）。
    """
    with db._LOCK:  # 持锁读
        rows = db.get_conn().execute("SELECT * FROM region_flow").fetchall()  # 全量查询
    return [dict(r) for r in rows]  # Row 转普通字典

def attach_status(regions: list[dict]) -> list[dict]:  # 附加状态字段
    """给区域列表附加 ratio（占容量比）与 status（congested/normal/idle）。

    入参：regions —— 区域字典列表。
    出参：附加 ratio/status 后的新列表（不改入参）。
    失败行为：capacity 异常时按 1 兜底防除零。
    """
    congestion = CONFIG["simulation"]["congestion_ratio"]  # 拥堵阈值
    idle = CONFIG["simulation"]["idle_ratio"]              # 空区阈值
    out = []  # 输出列表
    for r in regions:  # 逐区域计算
        capacity = r.get("capacity") or 1  # 容量兜底，防除零
        ratio = r["current_count"] / capacity  # 占容量比
        status = "congested" if ratio >= congestion else ("idle" if ratio <= idle else "normal")  # 三态判定
        out.append({**r, "ratio": round(ratio, 4), "status": status})  # 附加字段
    return out  # 返回新列表

def role_view(role: str, regions: list[dict]) -> list[dict]:  # 角色视图裁剪
    """按角色权限对区域列表做行级（可见区域）与列级（敏感字段）双重裁剪。

    入参：role —— 角色ID；regions —— 富化后的全量区域列表。
    出参：该角色可见的字段裁剪后的区域列表。
    失败行为：未知角色返回空列表（默认拒绝）。
    """
    rows = visible_regions(role, regions)  # 行级过滤：类型不可见的直接剔除
    full_fields = {"id", "name", "type", "capacity", "current_count", "ratio", "status", "updated_at"}  # 全字段集
    # 列级裁剪策略：商户代表只给比例与状态，不给绝对人数与容量（避免推断全局敏感数据）
    if role == "shanghu_daibiao":  # 商户角色特殊列裁剪
        allowed = {"id", "name", "type", "ratio", "status", "updated_at"}  # 隐藏 current_count/capacity
    elif role == "rukou_yindao":  # 引导员只关心现场状态
        allowed = {"id", "name", "type", "ratio", "status"}  # 不给精确更新时间等运维字段
    else:  # 其余角色给全字段
        allowed = full_fields  # 全字段可见
    return [{k: r[k] for k in r if k in allowed} for r in rows]  # 双重裁剪后返回

# ---------- 业务接口 ----------
@app.get("/api/flow/overview")  # 全园客流总览
def flow_overview(role: str = Depends(get_role)):  # 依赖注入完成鉴权
    """按角色返回全园客流总览（行级+列级裁剪）。

    入参：Authorization 头（Bearer 令牌）。
    出参：统一结构体，data 含 role/regions/alerts。
    失败行为：令牌无效 401；未定义角色视图为空列表。
    """
    # 权限校验：overview 是基础功能，所有在册角色均可用；未知角色已在鉴权层拦截
    if not check_feature(role, "overview"):  # 双保险校验
        db.audit(role, "/api/flow/overview", False, "无 overview 功能权限")  # 越界留痕
        return fail("FORBIDDEN", f"角色[{ROLE_SCOPES[role]['name']}]无权访问客流总览")  # 拦截
    regions = attach_status(load_regions())  # 富化现状数据
    view = role_view(role, regions)  # 角色视图裁剪
    alerts = [r for r in view if r.get("status") in ("congested", "idle")]  # 该角色可见范围内的预警项
    db.audit(role, "/api/flow/overview", True)  # 放行留痕
    return ok({"role": ROLE_SCOPES[role]["name"], "regions": view,  # 角色名+区域列表
               "alerts": alerts, "ts": datetime.now().isoformat(timespec="seconds")})  # 预警+时间戳

@app.get("/api/flow/history/{region_id}")  # 区域历史趋势
def flow_history(region_id: str, role: str = Depends(get_role)):  # 鉴权
    """返回指定区域的近N条历史客流（仅限有 history 权限的角色）。

    入参：region_id 路径参数 + Bearer 令牌。
    出参：统一结构体，data 为历史点列表 [{count, ts}]。
    失败行为：无权限 403 结构体；区域不存在返回空列表（不报错泄露区域清单）。
    """
    # 权限校验：history 是敏感功能，只有运营/调度外勤需要趋势的角色可用
    # 商户代表只允许查商户片区、票务只允许查全园聚合——区域类型校验在下方
    if not check_feature(role, "history"):  # 功能权限校验
        db.audit(role, f"/api/flow/history/{region_id}", False, "无 history 权限")  # 越界留痕
        return fail("FORBIDDEN", f"角色[{ROLE_SCOPES[role]['name']}]无权查看历史趋势")  # 拦截
    region = next((r for r in CONFIG["simulation"]["regions"] if r["id"] == region_id), None)  # 查区域配置
    if region is None:  # 区域不存在
        return ok([])  # 返回空列表，不暴露区域存在性
    # 行级权限：商户只能查自己片区的趋势（commercial 类型）
    if role == "shanghu_daibiao" and region["type"] != "commercial":  # 商户越界查非商户区
        db.audit(role, f"/api/flow/history/{region_id}", False, "商户仅可查商户片区")  # 越界留痕
        return fail("FORBIDDEN", "商户服务代表仅可查看商户片区趋势")  # 拦截
    with db._LOCK:  # 持锁读历史
        rows = db.get_conn().execute(  # 取最近200个点，前端画趋势图足够
            "SELECT count, ts FROM flow_history WHERE region_id=? ORDER BY seq DESC LIMIT 200",
            (region_id,)).fetchall()  # 参数化查询
    db.audit(role, f"/api/flow/history/{region_id}", True)  # 放行留痕
    return ok([{"count": r["count"], "ts": r["ts"]} for r in reversed(rows)])  # 按时间正序返回

@app.get("/api/ticket/stats")  # 票务聚合数据
def ticket_stats(role: str = Depends(get_role)):  # 鉴权
    """返回近期票务出票/入园聚合统计（仅运营主任与票务数据工程师可见）。

    入参：Bearer 令牌。
    出参：统一结构体，data 为 [{ts, issued, entered}]。
    失败行为：无权限 403；无数据返回空列表。
    """
    # 权限校验：票务数据属于独立数据域（报告 f30：各系统权限独立），
    # 只有票务工程师（本职）与运营主任（全权限）可读，其余岗位一律拦截
    if not check_feature(role, "ticket"):  # 功能校验
        db.audit(role, "/api/ticket/stats", False, "无 ticket 权限")  # 越界留痕
        return fail("FORBIDDEN", f"角色[{ROLE_SCOPES[role]['name']}]无权查看票务数据")  # 拦截
    with db._LOCK:  # 持锁读
        rows = db.get_conn().execute(  # 取最近100条聚合
            "SELECT ts, issued, entered FROM ticket_stats ORDER BY ts DESC LIMIT 100").fetchall()  # 查询
    db.audit(role, "/api/ticket/stats", True)  # 放行留痕
    return ok([dict(r) for r in reversed(rows)])  # 时间正序返回

@app.get("/api/dispatch/suggestions")  # 调度建议列表
def dispatch_suggestions(role: str = Depends(get_role)):  # 鉴权
    """生成并返回当前调度建议（仅运营主任与调度员可见）。

    入参：Bearer 令牌。
    出参：统一结构体，data 为建议列表（每条含 requires_human 与 safety_note）。
    失败行为：无权限 403；引擎异常向上抛由框架兜底。
    """
    # 权限校验：建议的生成与接收仅限调度链路岗位（运营主任统筹、调度员接收）；
    # 引导员只接收"已确认的指令"（阶段2），不直接看建议池，防止多头指挥
    if not check_feature(role, "suggestions"):  # 功能校验
        db.audit(role, "/api/dispatch/suggestions", False, "无 suggestions 权限")  # 越界留痕
        return fail("FORBIDDEN", f"角色[{ROLE_SCOPES[role]['name']}]无权获取调度建议")  # 拦截
    suggestions = build_suggestions(load_regions())  # 规则引擎生成建议
    db.audit(role, "/api/dispatch/suggestions", True, f"共{len(suggestions)}条")  # 放行留痕
    return ok(suggestions)  # 返回

@app.post("/api/dispatch/confirm")  # 人工确认调度
async def dispatch_confirm(request: Request, role: str = Depends(get_role)):  # 鉴权
    """人工确认一条调度建议并写入防篡改留痕（仅运营主任可确认）。

    入参：JSON 体 {"suggestion_id":..., "action":..., "region_id":..., "reason":...} + Bearer 令牌。
    出参：统一结构体，data 为留痕单号。
    失败行为：无权限 403；参数缺失 422（FastAPI 校验）。
    """
    # 权限校验：确认权是调度闭环的最后一道闸（报告 f23：确认接手才算完成），
    # 阶段1只给运营主任；调度员建议、确认权分离，防止自说自话
    if not check_feature(role, "confirm"):  # 功能校验
        db.audit(role, "/api/dispatch/confirm", False, "无 confirm 权限")  # 越界留痕
        return fail("FORBIDDEN", "调度确认仅限运营主任审批")  # 拦截
    body = await request.json()  # 解析 JSON 体
    sug_id = body.get("suggestion_id", "")  # 建议编号
    action = body.get("action", "")  # 确认的动作
    region_id = body.get("region_id", "")  # 涉及区域
    reason = body.get("reason", "人工确认")  # 确认理由
    if not (sug_id and action and region_id):  # 必填校验
        return fail("BAD_REQUEST", "suggestion_id/action/region_id 为必填")  # 400 语义
    # 安全审查：确认的动作不允许出现在禁止词黑名单（红线：不自动限流疏散，
    # 人工确认的动作也必须是人类可执行的人员引导，不是系统自动执行）
    from backend.scheduler_agent import _FORBIDDEN_ACTIONS  # 引入黑名单
    if any(w in action for w in _FORBIDDEN_ACTIONS):  # 动作命中禁止词
        db.audit(role, "/api/dispatch/confirm", False, f"动作含禁止词: {action}")  # 拦截留痕
        return fail("SAFETY_BLOCKED", "该动作触碰安全红线（限流/疏散类），系统不予受理")  # 硬拦截
    with db._LOCK:  # 持锁写留痕
        conn = db.get_conn()  # 取连接
        cur = conn.execute(  # 插入留痕单（status=confirmed，confirmed_by=确认人）
            """INSERT INTO dispatch_log(ts,region_id,action,status,suggested_by,confirmed_by,reason)
               VALUES(?,?,?,?,?,?,?)""",  # 七字段
            (datetime.now().isoformat(timespec="seconds"), region_id, action,  # 时间/区域/动作
             "confirmed", sug_id, ROLE_SCOPES[role]["name"], reason))  # 状态/来源/确认人/理由
        log_id = cur.lastrowid  # 留痕单号
        conn.commit()  # 提交
    db.audit(role, "/api/dispatch/confirm", True, f"留痕单号{log_id}")  # 放行留痕
    return ok({"log_id": log_id, "msg": "调度确认已留痕（不可篡改）"})  # 返回单号

@app.get("/api/dispatch/log")  # 调度留痕查询
def dispatch_log(role: str = Depends(get_role)):  # 鉴权
    """返回调度留痕记录（仅运营主任可见，审计视角）。

    入参：Bearer 令牌。
    出参：统一结构体，data 为留痕列表（时间倒序，最多200条）。
    失败行为：无权限 403。
    """
    # 权限校验：留痕是管理审计数据，只有运营主任全权限可见；
    # 其余岗位看不到他人责任记录，避免岗位间信息越界（红线3）
    if not check_feature(role, "confirm"):  # 复用 confirm 权限位表示审计能力
        db.audit(role, "/api/dispatch/log", False, "无留痕查看权限")  # 越界留痕
        return fail("FORBIDDEN", "调度留痕仅限运营主任查看")  # 拦截
    with db._LOCK:  # 持锁读
        rows = db.get_conn().execute(  # 查询留痕
            "SELECT * FROM dispatch_log ORDER BY id DESC LIMIT 200").fetchall()  # 倒序200条
    db.audit(role, "/api/dispatch/log", True)  # 放行留痕
    return ok([dict(r) for r in rows])  # 返回

# ---------- WebSocket 实时推送 ----------
@app.websocket("/ws/flow")  # WS 端点
async def ws_flow(ws: WebSocket):  # 连接处理
    """WebSocket 实时推送角色化客流总览，周期与模拟器刷新周期一致。

    入参：query 参数 token —— 岗位访问令牌。
    出参：周期性推送统一结构体（同 /api/flow/overview 的 data）。
    失败行为：令牌无效立即关闭连接（1008 策略违规）；客户端断开自动退出循环。
    """
    token = ws.query_params.get("token", "")  # 从查询串取令牌（WS 无法自定义头，用 query 传递）
    role = resolve_role(token)  # 令牌换角色
    if role is None:  # 无效令牌
        await ws.close(code=1008)  # 1008=策略违规，直接拒绝
        return  # 结束
    await ws.accept()  # 接受连接
    try:  # 循环推送
        while True:  # 持续推送直到断开
            regions = attach_status(load_regions())  # 每轮重新取数
            view = role_view(role, regions)  # 角色裁剪（WS 与 HTTP 同一权限模型，不旁路）
            alerts = [r for r in view if r.get("status") in ("congested", "idle")]  # 预警项
            await ws.send_json({"ok": True,  # WS 消息也用统一结构
                                "data": {"role": ROLE_SCOPES[role]["name"], "regions": view,  # 角色数据
                                         "alerts": alerts, "ts": datetime.now().isoformat(timespec="seconds")},  # 预警+时间
                                "error": None})  # 无错误
            import asyncio  # 局部引入异步库
            await asyncio.sleep(CONFIG["simulation"]["interval_seconds"])  # 与模拟器同频
    except WebSocketDisconnect:  # 客户端断开
        return  # 正常退出

@app.get("/api/health")  # 健康检查
def health():  # 处理函数
    """健康检查端点（无需鉴权，供探活）。入参：无；出参：统一结构体；无失败分支。"""
    return ok({"service": "qinglu-flow-agent", "time": datetime.now().isoformat(timespec="seconds")})  # 服务名+时间
