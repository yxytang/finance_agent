"""常量、路径、模型工厂。

所有路径都相对项目根目录，**模型不接收任何路径** —— 读账单是 MCP server
内部的事，读知识库是 ingest 的事。所以这个 repo 里没有 forge 那套工作区边界
判定：没有入口，就不需要守门。
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)

DATA_DIR = PROJECT_ROOT / "data"
SKILLS_DIR = PROJECT_ROOT / "skills"
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
MEMORY_DIR = PROJECT_ROOT / "memory"

TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
REFERENCE_CSV = DATA_DIR / "reference.csv"
CATEGORY_CACHE = DATA_DIR / "category_cache.json"

# 固定类目表。**必须固定**，不能让模型自由生成 —— 类目一变，per-class
# precision/recall 就没法算了，第八天的分类评测也就无从谈起。
CATEGORIES = (
    "房租",
    "水电燃气",
    "通讯",
    "超市",
    "餐饮外卖",
    "咖啡",
    "交通",
    "购物",
    "订阅",
    "医疗",
    "娱乐",
    "旅行",
    "其他",
)

# 单轮对话里模型最多能连续调用工具多少次。超了就停 —— 否则模型陷入循环时
# 会一直烧钱，而用户只看到一个不动的光标。
MAX_STEPS = 24

# 单次工具输出上限。查询结果很容易几百行，不该指望后面的上下文压缩来兜。
MAX_TOOL_OUTPUT = 20_000

# 走 LLM 分类时一次批量送多少笔。太少了浪费往返，太多了模型容易漏项 ——
# 返回的 JSON 必须能和输入一一对上。
CATEGORIZE_BATCH = 25

# skill 清单进 system prompt 的字符预算。
# 清单在 messages[0] 里，也就是**每一轮都在**，所以它必须有硬上限。
# 超预算的 skill 整条不列（原因见 prompt.render_skills_section）。
SKILL_LIST_BUDGET = 1500

# 第三级资源文件（references/、scripts/）单个的大小上限。
MAX_RESOURCE = 60_000

REQUEST_TIMEOUT = 120


def build_model(**kwargs) -> ChatOpenAI:
    """构造模型。DeepSeek 走 OpenAI 兼容接口。

    没 key 就直接报错，而不是构造出一个每次调用都 401 的客户端 ——
    让配置问题在启动时暴露，而不是等第一次对话。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"没找到 DEEPSEEK_API_KEY，请填进 {ENV_FILE}")

    return ChatOpenAI(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        api_key=api_key,
        temperature=0,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
