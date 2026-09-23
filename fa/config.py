"""常量、路径、模型工厂。

所有路径都相对项目根目录，**模型不接收任何路径** —— 读账单是 MCP server
内部的事，读知识库是 ingest 的事。所以这个 repo 里没有 forge 那套工作区边界
判定：没有入口，就不需要守门。
"""

import os
from dataclasses import dataclass
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
# 检索索引的落盘位置。放在 knowledge/.index/ 而不是 data/：它和语料是一体的，
# 语料换地方了索引也该跟着走。已在 .gitignore 里 —— 它是产物，能重建。
KNOWLEDGE_INDEX = KNOWLEDGE_DIR / ".index" / "index.json"
# 向量库的落盘位置。和 index.json 同一个目录，理由也一样：它是**这份语料的**
# 索引，不是一份独立的数据。已经在 .gitignore 里（`knowledge/.index/` 整条），
# 因为它同样是产物、能重建。
CHROMA_DIR = KNOWLEDGE_DIR / ".index" / "chroma"
MEMORY_DIR = PROJECT_ROOT / "memory"

TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
REFERENCE_CSV = DATA_DIR / "reference.csv"
CATEGORY_CACHE = DATA_DIR / "category_cache.json"

# 跨会话记忆。放在 memory/ 而不是 data/：前者是「agent 知道的关于用户的事」，
# 后者是「用户的账单数据」。混在一起之后，清理账单和清理记忆就分不开了。
MEMORY_FILE = MEMORY_DIR / "facts.md"

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

# 子 agent 的步数上限，比主 agent 低得多。
#
# 子 agent 的任务应该是**聚焦的**（「把餐饮这一类逐月看一下」），不是开放的。
# 上限给低，是为了让「任务没切好」这件事**尽早暴露**：一个切得对的问题几步就
# 回来了，切得太宽的问题会撞上限然后带着一句「我没做完」返回 —— 而那是主 agent
# 需要知道的信号，不该被一个宽松的上限掩盖掉。
SUBAGENT_MAX_STEPS = 10

# 单次工具输出上限。查询结果很容易几百行，不该指望后面的上下文压缩来兜。
MAX_TOOL_OUTPUT = 20_000

# 分组明细最多列几组。商户分组在这份数据上有几十组，全列进来会把上下文吃掉
# 一大块，而模型真正要看的是头部。超出的部分会说明「另有 N 组未列出」——
# 不能悄悄截断，否则模型会以为这就是全部。
MAX_QUERY_GROUPS = 40

# 异常检测每次最多列几条明细，理由同上。
MAX_ANOMALY_ITEMS = 20

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


def build_model(cls=ChatOpenAI, **kwargs) -> ChatOpenAI:
    """构造模型。DeepSeek 走 OpenAI 兼容接口。

    没 key 就直接报错，而不是构造出一个每次调用都 401 的客户端 ——
    让配置问题在启动时暴露，而不是等第一次对话。

    `cls` 可以换成别的实现（比如带重试的包装），用来应对**同一套配置**下的
    不同健壮性需求。默认就是原生的 ChatOpenAI。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"没找到 DEEPSEEK_API_KEY，请填进 {ENV_FILE}")

    return cls(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        api_key=api_key,
        temperature=0,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )


# embedding 走 OpenAI 兼容接口，默认阿里云百炼（DashScope）。
DEFAULT_EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBED_MODEL = "text-embedding-v4"

# 重排。**端点和 embedding 不是同一个** —— embedding 是 OpenAI 兼容形状，重排是
# 百炼自己那套（`input` / `parameters` 包一层，回 `output.results`）。
#
# 这两个值是**实测过的**，不是照文档抄的（2026-09-23：拿真 key 打了一次，
# 200，`relevance_score` 把正确答案打在 0.174、其余两篇 0.006）。留空就关掉重排。
DEFAULT_RERANK_BASE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
DEFAULT_RERANK_MODEL = "gte-rerank-v2"


@dataclass(frozen=True)
class RagSettings:
    """向量检索要的配置。

    embedding 和 rerank **分开配**，理由见上面那两段。
    """

    api_key: str
    embed_base_url: str
    embed_model: str
    rerank_base_url: str
    rerank_model: str

    @property
    def rerank_enabled(self) -> bool:
        """URL 和模型都填了才算启用重排。

        想关掉重排就把这两个变量之一置空（`RAG_RERANK_MODEL=`）。缺一个就退回
        融合顺序（降级，不是报错）—— 重排是锦上添花，配不全不该让检索整个
        不可用。
        """
        return bool(self.rerank_base_url and self.rerank_model)


def rag_settings() -> RagSettings | None:
    """读向量检索的配置。**没配就返回 None，不报错。**

    这里**故意**和 `build_model()` 反着来。那个缺 key 必须当场报，因为没有模型
    整个 agent 一个字都答不出来；而向量检索是**加强项** —— 缺了它退回纯 BM25
    （也就是这个功能加进来之前的行为），agent 照常跑。

    把这里也做成报错，等于让一个可选功能把整个程序拦下来，而且是在一个用户
    根本没要求过它的场景里。所以「没配」是一个**正常返回值**，不是异常。

    后面 `if not settings` 的人要自己决定怎么处理：检索层是降级，而
    `--mode full` 那种「用户明说要全开」的入口应该自己报错。
    """
    api_key = os.environ.get("RAG_API_KEY", "").strip()
    if not api_key:
        return None

    return RagSettings(
        api_key=api_key,
        embed_base_url=os.environ.get(
            "RAG_EMBED_BASE_URL", DEFAULT_EMBED_BASE_URL
        ).strip(),
        embed_model=os.environ.get("RAG_EMBED_MODEL", DEFAULT_EMBED_MODEL).strip(),
        rerank_base_url=os.environ.get(
            "RAG_RERANK_BASE_URL", DEFAULT_RERANK_BASE_URL
        ).strip(),
        rerank_model=os.environ.get("RAG_RERANK_MODEL", DEFAULT_RERANK_MODEL).strip(),
    )
