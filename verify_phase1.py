# -*- coding: utf-8 -*-
"""阶段1验证脚本：一键自测鉴权、角色隔离、越界拦截、留痕防篡改。

运行前提：后端已在 127.0.0.1:8000 启动（python -m uvicorn backend.main:app --port 8000）。
运行方式：python verify_phase1.py
"""
import sqlite3          # 直连数据库验证防篡改触发器
import requests         # HTTP 请求库

BASE = "http://127.0.0.1:8000"  # 后端地址（与 config.yaml server 段一致）

# 六岗位令牌（与 config.yaml auth.tokens 一致，便于人肉比对）
TOK_OPS = "tok-ops-gulanlan-2026"        # 运营主任
TOK_GUIDE = "tok-guide-linyizhou-2026"   # 入口引导员
TOK_DISPATCH = "tok-dispatch-tangyingqiu-2026"  # 调度员
TOK_TICKET = "tok-ticket-qinrui-2026"    # 票务工程师
TOK_SAFETY = "tok-safety-fujing-2026"    # 安全负责人
TOK_MERCHANT = "tok-merchant-yeshu-2026" # 商户代表

PASS, FAIL = [], []  # 用例结果收集

def check(name: str, cond: bool, detail: str = "") -> None:  # 断言记录
    """记录一条用例结果并打印。入参 name=用例名, cond=是否通过, detail=补充；无失败分支。"""
    (PASS if cond else FAIL).append(name)  # 归档
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))  # 打印

def get(path: str, token: str) -> dict:  # GET 帮助函数
    """带令牌请求 GET 接口并返回 JSON。入参 path=路径, token=令牌；失败抛 requests 异常。"""
    r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=10)  # 请求
    return r.json()  # 解析 JSON

print("== 1. 健康检查 ==")  # 分组标题
health = requests.get(f"{BASE}/api/health", timeout=10).json()  # 无鉴权探活
check("健康检查返回 ok", health.get("ok") is True)  # 断言

print("== 2. 鉴权 ==")  # 分组标题
bad = get("/api/flow/overview", "tok-invalid-xxx")  # 无效令牌
check("无效令牌被 401 拦截", bad.get("ok") is False and bad.get("error", {}).get("code") == "UNAUTHORIZED", str(bad.get("error")))  # 断言

print("== 3. 角色数据隔离（红线3）==")  # 分组标题
ops = get("/api/flow/overview", TOK_OPS)  # 运营主任：全园
check("运营主任可见全部区域", ops["ok"] and len(ops["data"]["regions"]) >= 7, f"可见{len(ops['data']['regions'])}个区域")  # 断言
guide = get("/api/flow/overview", TOK_GUIDE)  # 引导员
types_guide = {r["type"] for r in guide["data"]["regions"]}  # 引导员可见类型
check("引导员只看到入口区域", guide["ok"] and types_guide == {"entrance"}, f"可见类型={types_guide}")  # 断言
merchant = get("/api/flow/overview", TOK_MERCHANT)  # 商户
types_m = {r["type"] for r in merchant["data"]["regions"]}  # 商户可见类型
has_count = any("current_count" in r for r in merchant["data"]["regions"])  # 是否泄露绝对人数
check("商户只看到商户片区", merchant["ok"] and types_m == {"commercial"}, f"可见类型={types_m}")  # 断言
check("商户视图不含绝对人数（列级裁剪）", not has_count)  # 断言
merchant_hist = get("/api/flow/history/entrance_north", TOK_MERCHANT)  # 商户越界查入口历史
check("商户查入口历史被 403 拦截", merchant_hist.get("ok") is False and merchant_hist["error"]["code"] == "FORBIDDEN", str(merchant_hist.get("error")))  # 断言
guide_sug = get("/api/dispatch/suggestions", TOK_GUIDE)  # 引导员越界要建议
check("引导员获取调度建议被拦截", guide_sug.get("ok") is False, str(guide_sug.get("error")))  # 断言
safety_ticket = get("/api/ticket/stats", TOK_SAFETY)  # 安全负责人越界看票务
check("安全负责人查票务被拦截", safety_ticket.get("ok") is False, str(safety_ticket.get("error")))  # 断言

print("== 4. 调度建议与人工确认（红线5）==")  # 分组标题
sugs = get("/api/dispatch/suggestions", TOK_DISPATCH)  # 调度员取建议
check("调度员可获取调度建议", sugs["ok"] and len(sugs["data"]) > 0, f"共{len(sugs.get('data', []))}条建议")  # 断言
if sugs.get("data"):  # 有建议才测确认
    first = sugs["data"][0]  # 取第一条
    conf = requests.post(f"{BASE}/api/dispatch/confirm",  # 确认接口
        headers={"Authorization": f"Bearer {TOK_OPS}"},  # 运营主任确认
        json={"suggestion_id": first["id"], "action": first["action"],  # 建议信息
              "region_id": first["region_id"], "reason": "阶段1自测确认"}, timeout=10)  # 理由
    conf_json = conf.json()  # 解析
    check("运营主任确认建议并生成留痕单", conf_json.get("ok") is True, f"单号={conf_json.get('data', {}).get('log_id')}")  # 断言
    log = get("/api/dispatch/log", TOK_OPS)  # 查留痕
    check("留痕可在审计接口查到", log["ok"] and any(l["suggested_by"] == first["id"] for l in log["data"]))  # 断言
    guide_conf = requests.post(f"{BASE}/api/dispatch/confirm",  # 引导员越界确认
        headers={"Authorization": f"Bearer {TOK_GUIDE}"},  # 引导员令牌
        json={"suggestion_id": "x", "action": "引导分流", "region_id": "garden_lake"}, timeout=10)  # 参数
    check("引导员确认调度被 403 拦截", guide_conf.json().get("ok") is False, str(guide_conf.json().get("error")))  # 断言
    forbid = requests.post(f"{BASE}/api/dispatch/confirm",  # 尝试确认禁止动作
        headers={"Authorization": f"Bearer {TOK_OPS}"},  # 运营主任令牌
        json={"suggestion_id": "x", "action": "自动限流疏散", "region_id": "entrance_north"}, timeout=10)  # 禁止词
    check("限流/疏散类动作被安全红线拦截", forbid.json().get("ok") is False and forbid.json()["error"]["code"] == "SAFETY_BLOCKED", str(forbid.json().get("error")))  # 断言

def _try_forbidden(sql: str, expect_msg: str, name: str) -> None:  # 防篡改用例公共逻辑
    """直连数据库尝试篡改/删除留痕，断言被触发器拦截。

    入参：sql=攻击语句；expect_msg=触发器报错文案；name=用例名。
    出参：无（结果记入 PASS/FAIL）。
    失败行为：语句意外成功或锁死均判 FAIL；无论成败都关闭连接释放写锁。
    """
    conn = None  # 预置连接句柄，便于 finally 释放
    try:  # 期望触发器 ABORT 抛异常
        conn = sqlite3.connect("data/qinglu.db", timeout=5)  # 直连库绕过接口，timeout=5 等待服务端短暂写锁
        conn.execute(sql)  # 尝试执行篡改/删除语句
        check(name, False, "语句竟然执行成功了")  # 没抛异常=触发器未生效
    except sqlite3.DatabaseError as exc:  # 触发器中止
        check(name, expect_msg in str(exc), str(exc))  # 断言报错文案匹配
    finally:  # 无论成败都必须释放连接
        if conn is not None:  # 连接已建立
            conn.rollback()  # 回滚残留事务（RAISE(ABORT) 后事务未自动结束，不回滚会占住写锁）
            conn.close()  # 关闭连接，避免锁死后续用例

print("== 5. 留痕防篡改（数据库层触发器）==")  # 分组标题
_try_forbidden("UPDATE dispatch_log SET action='篡改测试'", "不可篡改", "篡改留痕被触发器拦截")  # 篡改用例
_try_forbidden("DELETE FROM dispatch_log", "不可删除", "删除留痕被触发器拦截")  # 删除用例

print()  # 空行
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")  # 汇总
if FAIL:  # 有失败
    print("失败用例：", FAIL)  # 列出
    raise SystemExit(1)  # 非零退出码便于 CI 识别
print("阶段1自测全部通过 ✔")  # 收尾
