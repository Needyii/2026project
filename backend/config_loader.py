# -*- coding: utf-8 -*-
"""配置加载模块：统一从 config.yaml 读取全局配置，代码中不出现任何硬编码地址或密钥。"""
import yaml          # 用于解析 YAML 配置文件
from pathlib import Path  # 用于跨平台路径处理

# 项目根目录 = backend 目录的上一级（config.yaml 与 backend/ 同级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # 解析出绝对路径，避免工作目录不同导致找不到配置

def load_config() -> dict:  # 对外暴露的配置加载函数
    """读取项目根目录下的 config.yaml 并返回字典。

    入参：无。
    出参：dict —— 与 config.yaml 结构一致的配置字典。
    失败行为：文件缺失或 YAML 语法错误时直接抛出异常终止启动（配置错误不允许带病运行）。
    """
    cfg_path = PROJECT_ROOT / "config.yaml"  # 拼出配置文件绝对路径
    with open(cfg_path, "r", encoding="utf-8") as f:  # 以 UTF-8 打开，保证中文注释与键值正常读取
        return yaml.safe_load(f)  # safe_load 只解析数据不执行任意对象，防止配置注入

# 模块级单例：其余模块 import CONFIG 即可获得配置，避免重复读盘
CONFIG = load_config()  # 启动时加载一次；运行期配置变更需重启服务生效
