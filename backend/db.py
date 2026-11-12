# -*- coding: utf-8 -*-
"""数据库层：SQLite 建表、防篡改触发器、审计写入。

数据红线落地：
  红线1/2 —— 所有表只有区域聚合人数，无任何个人字段；
  红线5   —— dispatch_log 用触发器禁止 UPDATE/DELETE，调度记录只增不改；
  红线6   —— 不提供任何导出原始明细的函数，本模块不实现 export 类接口。
"""
import sqlite3               # Python 标准库 SQLite 驱动，零额外部署
import threading             # 多线程读写锁，模拟器线程与接口线程并发访问数据库
from datetime import datetime  # 生成留痕时间戳
from pathlib import Path     # 路径处理
from backend.config_loader import CONFIG, PROJECT_ROOT  # 读取库路径与项目根目录

# 数据库文件绝对路径：data/qinglu.db，目录不存在则自动创建
DB_PATH = PROJECT_ROOT / CONFIG["database"]["path"]  # 拼接绝对路径
DB_PATH.parent.mkdir(parents=True, exist_ok=True)    # 确保 data/ 目录存在

# 单连接 + 线程锁方案：SQLite 文件库不适合多连接高并发，演示系统一把锁最稳
_CONN: sqlite3.Connection | None = None  # 全局连接句柄，惰性初始化
_LOCK = threading.Lock()                 # 读写互斥锁：任何数据库操作前必须持有

def get_conn() -> sqlite3.Connection:  # 获取数据库连接
    """返回全局 SQLite 连接，首次调用时完成建表。

    入参：无。
    出参：sqlite3.Connection —— 开启了外键与行工厂的连接对象。
    失败行为：建表 SQL 错误直接抛异常终止（库结构错误不允许带病运行）。
    """
    global _CONN  # 声明使用全局连接
    if _CONN is None:  # 惰性初始化：第一次调用才建连接
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)  # check_same_thread=False 允许跨线程，靠 _LOCK 保护
        _CONN.execute("PRAGMA journal_mode=WAL")  # 开启 WAL 模式：读写不互斥，避免验证脚本直连时撞锁报 database is locked
        _CONN.row_factory = sqlite3.Row  # 行结果支持按列名取值，代码可读性更好
        _init_tables(_CONN)  # 连接建立后立即建表（幂等，IF NOT EXISTS）
    return _CONN  # 返回共享连接

def _init_tables(conn: sqlite3.Connection) -> None:  # 建表（内部函数）
    """创建全部业务表与防篡改触发器（幂等，可重复执行）。

    入参：conn —— SQLite 连接对象。
    出参：无（副作用：数据库中生成表结构）。
    失败行为：SQL 语法/权限错误抛异常向上传播。
    """
    cur = conn.cursor()  # 获取游标执行 DDL
    # ---- 表1：region_flow 区域客流聚合现状（只存聚合值，红线2）----
    cur.execute("""CREATE TABLE IF NOT EXISTS region_flow (
        id            TEXT PRIMARY KEY,   -- 区域ID（config.yaml 定义）
        name          TEXT NOT NULL,      -- 区域显示名
        type          TEXT NOT NULL,      -- 区域类型 entrance/garden/commercial
        capacity      INTEGER NOT NULL,   -- 区域容量（人）
        current_count INTEGER NOT NULL,   -- 当前聚合人数（人，非个体）
        updated_at    TEXT NOT NULL       -- 更新时间 ISO8601
    )""")  # 现状表：模拟器每周期覆盖写入
    # ---- 表2：flow_history 区域客流历史（画趋势图用）----
    cur.execute("""CREATE TABLE IF NOT EXISTS flow_history (
        seq       INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增序列
        region_id TEXT NOT NULL,          -- 区域ID
        count     INTEGER NOT NULL,       -- 该时刻聚合人数
        ts        TEXT NOT NULL           -- 采集时间 ISO8601
    )""")  # 追加写，配合 history_keep_hours 定期清理
    # ---- 表3：dispatch_log 调度留痕（红线5：不可篡改）----
    cur.execute("""CREATE TABLE IF NOT EXISTS dispatch_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 留痕单号
        ts            TEXT NOT NULL,      -- 记录时间
        region_id     TEXT NOT NULL,      -- 涉及区域
        action        TEXT NOT NULL,      -- 动作（如"引导分流至湖畔区"）
        status        TEXT NOT NULL,      -- suggested=建议 / confirmed=人工已确认
        suggested_by  TEXT NOT NULL,      -- 建议来源（rule_engine / llm）
        confirmed_by  TEXT,               -- 人工确认人（未确认时为空）
        reason        TEXT NOT NULL       -- 生成原因（拥堵/空区数据依据）
    )""")  # 只允许 INSERT，UPDATE/DELETE 由触发器禁止
    # ---- 防篡改触发器：任何 UPDATE/DELETE 直接中止 ----
    # 为什么这么卡：调度记录是责任认定与复盘的唯一依据（红线5），
    # 一旦可改可删，留痕就失去审计意义，所以数据库层硬禁止，而非仅靠接口层约束
    cur.execute("""CREATE TRIGGER IF NOT EXISTS dispatch_log_no_update
        BEFORE UPDATE ON dispatch_log
        BEGIN SELECT RAISE(ABORT, '调度留痕不可篡改'); END""")  # 拦截 UPDATE
    cur.execute("""CREATE TRIGGER IF NOT EXISTS dispatch_log_no_delete
        BEFORE DELETE ON dispatch_log
        BEGIN SELECT RAISE(ABORT, '调度留痕不可删除'); END""")  # 拦截 DELETE
    # ---- 表4：access_audit 访问审计（红线3：越界请求留证据）----
    cur.execute("""CREATE TABLE IF NOT EXISTS access_audit (
        seq     INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增序列
        ts      TEXT NOT NULL,      -- 请求时间
        role    TEXT,               -- 解析出的角色（未认证为 NULL）
        path    TEXT NOT NULL,      -- 请求路径
        allowed INTEGER NOT NULL,   -- 1=放行 0=拦截
        detail  TEXT                -- 拦截原因等补充信息
    )""")  # 谁、什么时候、碰了什么、放没放行，全部留痕
    conn.commit()  # 提交 DDL

def audit(role: str | None, path: str, allowed: bool, detail: str = "") -> None:  # 审计写入
    """写一条访问审计记录（所有接口调用前都会触发，含越界拦截记录）。

    入参：role —— 角色ID（未认证传 None）；path —— 请求路径；
         allowed —— 是否放行；detail —— 补充说明（如拦截原因）。
    出参：无。
    失败行为：写库失败仅打印告警不抛异常——审计失败不应阻塞业务主流程。
    """
    try:  # 审计失败不影响主业务
        with _LOCK:  # 持锁写库
            conn = get_conn()  # 取连接
            conn.execute(  # 追加审计行
                "INSERT INTO access_audit(ts, role, path, allowed, detail) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), role, path,
                 1 if allowed else 0, detail),  # allowed 转 0/1 存储
            )  # 参数化查询防注入
            conn.commit()  # 提交事务
    except Exception as exc:  # 捕获所有写库异常
        print(f"[audit] 审计写入失败: {exc}")  # 仅告警，不向上抛

def clean_old_history(keep_hours: int) -> int:  # 历史清理
    """清理超过保留时长的 flow_history 旧数据，防止库无限膨胀。

    入参：keep_hours —— 保留小时数（来自 config.yaml）。
    出参：int —— 本次删除的行数。
    失败行为：异常打印告警并返回 0，不阻塞模拟主循环。
    """
    cutoff = datetime.now().isoformat(timespec="seconds")  # 当前时间，简化处理：按字符串比较
    try:  # 清理失败不影响主流程
        with _LOCK:  # 持锁
            conn = get_conn()  # 取连接
            cur = conn.execute(  # 删除旧数据
                "DELETE FROM flow_history WHERE ts < datetime(?, ?)",
                (cutoff, f"-{keep_hours} hours"),  # 用 SQLite datetime 修饰符计算截止时间
            )  # 参数化删除
            conn.commit()  # 提交
            return cur.rowcount  # 返回删除行数
    except Exception as exc:  # 捕获异常
        print(f"[db] 历史清理失败: {exc}")  # 告警
        return 0  # 返回0表示本次未清理
