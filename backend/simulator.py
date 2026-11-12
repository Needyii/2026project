# -*- coding: utf-8 -*-
"""客流模拟器：后台线程持续生成"入口拥堵 + 园内空区"的聚合客流数据。

用途：真实景区数据源（票务平台/客流计数）尚未接入，本模拟器按
业务挖掘报告的真实基线（入口等待55分钟、园内空置）合成数据，
接口协议与真实数据源一致，后续替换数据源只改本文件。
红线1/2：模拟的只有"区域聚合人数"，不含任何个人轨迹或身份信息。
"""
import math        # 正弦波模拟潮汐客流
import random      # 随机扰动让数据更真实
import threading   # 后台线程
import time        # 周期控制
from datetime import datetime  # 时间戳
from backend.config_loader import CONFIG          # 全局配置
from backend.db import get_conn, _LOCK, clean_old_history  # 数据库写入与锁

_stop_event = threading.Event()  # 线程停止信号：服务关闭时通知模拟线程退出
_thread: threading.Thread | None = None  # 模拟线程句柄
_tick = 0  # 全局节拍计数：用于控制低频任务（票据聚合/历史清理）

def _gen_count(region: dict, t: float) -> int:  # 单区域人数生成
    """根据区域类型与时间生成当前聚合人数。

    入参：region —— 区域配置字典（含 type/capacity/base）；t —— 相对时间（秒）。
    出参：int —— 该区域当前人数（聚合值）。
    失败行为：类型未知时按 garden 兜底处理，不抛异常中断模拟。
    """
    base = region["base"]  # 基础人数
    # 潮汐系数：0.85~1.15 的缓变正弦波，模拟客流的自然涨落
    tide = 1.0 + 0.15 * math.sin(t / 60.0 + hash(region["id"]) % 628 / 100.0)  # 用区域ID哈希做相位差，各区域不同步
    noise = random.uniform(-0.04, 0.04)  # 4% 以内的随机噪声
    count = base * tide * (1 + noise)  # 合成人数
    if region["type"] == "entrance":  # 入口区域额外叠加高峰系数
        # 业务背景：入口长时间高水位（对应"入口等待55分钟"基线），
        # 潮汐之上再加 8% 系数，确保入口大概率越过 0.8 拥堵阈值，便于演示预警
        count *= 1.08  # 入口高峰抬升
    return max(0, int(count))  # 人数不为负，取整

def _write_tick(regions: list[dict], now: str) -> None:  # 单节拍写入
    """把本轮所有区域的人数写入现状表与历史表。

    入参：regions —— 区域配置列表；now —— 当前时间字符串。
    出参：无。
    失败行为：任何写库异常打印告警并跳过本轮，不中断模拟线程。
    """
    try:  # 单轮失败不影响下一轮
        with _LOCK:  # 持锁批量写
            conn = get_conn()  # 取连接
            for region in regions:  # 逐区域写入
                count = _gen_count(region, time.time())  # 生成当前人数
                # 现状表 UPSERT：有则更新无则插入（现状永远是最新值）
                conn.execute("""INSERT INTO region_flow(id,name,type,capacity,current_count,updated_at)
                                VALUES(?,?,?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET
                                current_count=excluded.current_count, updated_at=excluded.updated_at""",
                             (region["id"], region["name"], region["type"],
                              region["capacity"], count, now))  # 参数化写入
                conn.execute(  # 历史表追加一行
                    "INSERT INTO flow_history(region_id,count,ts) VALUES(?,?,?)",
                    (region["id"], count, now))  # 参数化写入
            conn.commit()  # 提交本轮
    except Exception as exc:  # 捕获本轮所有异常
        print(f"[simulator] 写入失败，跳过本轮: {exc}")  # 告警但继续

def _write_ticket_stats(now: str) -> None:  # 票务聚合（低频）
    """按周期生成票务出票/入园聚合数据（供票务数据工程师视图）。

    入参：now —— 当前时间字符串。
    出参：无。
    失败行为：异常打印告警，不影响客流模拟主循环。
    """
    # 说明：票务统计属于独立数据域，模拟为"每15分钟聚合一次"（对齐报告 f34 的更新周期）
    issued = random.randint(180, 320)   # 本时段出票数（聚合值，无个人信息）
    entered = int(issued * random.uniform(0.85, 0.95))  # 入园数略低于出票（有等待）
    try:  # 写库异常不中断主循环
        with _LOCK:  # 持锁
            conn = get_conn()  # 取连接
            conn.execute("""CREATE TABLE IF NOT EXISTS ticket_stats (
                ts TEXT PRIMARY KEY,   -- 统计时段起点
                issued INTEGER NOT NULL,  -- 出票数（聚合）
                entered INTEGER NOT NULL  -- 入园数（聚合）
            )""")  # 惰性建表：首次写票务数据时创建
            conn.execute(  # 追加本时段统计
                "INSERT OR REPLACE INTO ticket_stats(ts,issued,entered) VALUES(?,?,?)",
                (now, issued, entered))  # 参数化写入
            conn.commit()  # 提交
    except Exception as exc:  # 捕获异常
        print(f"[simulator] 票务聚合失败: {exc}")  # 告警

def _loop() -> None:  # 模拟主循环（线程体）
    """模拟线程主循环：按配置周期持续写入客流数据并做周期性维护。

    入参：无。
    出参：无。
    失败行为：循环体内所有业务异常均被捕获打印，线程本身只响应停止信号退出。
    """
    interval = CONFIG["simulation"]["interval_seconds"]  # 刷新周期（秒）
    regions = CONFIG["simulation"]["regions"]  # 区域配置
    keep_hours = CONFIG["simulation"]["history_keep_hours"]  # 历史保留时长
    while not _stop_event.is_set():  # 未收到停止信号则持续运行
        now = datetime.now().isoformat(timespec="seconds")  # 当前时间戳
        _write_tick(regions, now)  # 写本轮客流
        global _tick  # 修改模块级节拍计数
        _tick += 1  # 节拍推进
        if _tick % 5 == 0:  # 每5个节拍（约15秒）做一次票务聚合，模拟"15分钟一聚合"的加速演示
            _write_ticket_stats(now)  # 写票务统计
        if _tick % 600 == 0:  # 每600个节拍清理一次过期历史
            clean_old_history(keep_hours)  # 清理旧数据
        _stop_event.wait(interval)  # 等待下一周期（wait 可被停止信号提前唤醒）

def start() -> None:  # 启动模拟器
    """启动后台客流模拟线程（幂等：重复调用不会起第二个线程）。

    入参：无。
    出参：无。
    失败行为：线程创建失败抛异常向上传播（服务无法提供数据，应终止启动）。
    """
    global _thread  # 修改模块级线程句柄
    if _thread is not None and _thread.is_alive():  # 已在运行
        return  # 幂等返回，防止重复启动
    _stop_event.clear()  # 复位停止信号
    _thread = threading.Thread(target=_loop, name="flow-simulator", daemon=True)  # 守护线程：主进程退出自动回收
    _thread.start()  # 启动线程

def stop() -> None:  # 停止模拟器
    """停止后台模拟线程（服务关闭时调用，保证优雅退出）。

    入参：无。
    出参：无。
    失败行为：无异常抛出，最多等待一个周期后线程自行结束。
    """
    _stop_event.set()  # 设置停止信号，线程将在本周期结束后退出
