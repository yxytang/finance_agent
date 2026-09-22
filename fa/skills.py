"""skill 的发现与三级渐进披露。

「三级」说的是**进上下文**的量分三次涨，不是分三次读磁盘：

  1. `discover()` 只把每个 SKILL.md 的 name + description 放进 system prompt。
     skill 再多，进上下文的也只有这两样。
  2. 模型判断某个描述和当前任务相关，调 `use_skill(name=...)`，正文才进来。
  3. 正文提到的 `references/` / `scripts/` 文件，要再调一次
     `use_skill(name=..., path=...)` 才读进来。

第 3 级为什么不复用别的读取工具：**skill 属于 agent，不属于它操作的数据**。
skill 目录挂在项目根下，而 agent 分析的是 `data/` 里的账单 —— 两者没有从属
关系。所以资源的读取由 skill 自己负责，边界也由它自己再守一遍 —— 用的是
从 forge 带过来那条教训：**判定必须发生在 `resolve()` 之后**。

格式细节（对着 Claude Code 的 skill 文档核实过，几个容易记错的点）：

  · **所有 frontmatter 字段都是可选的**，没有一个是必填
  · `name` 省略时取**目录名**
  · `description` 省略时取正文第一行
  · frontmatter 只在 `---` 位于**文件第一行**时才成立，否则整份文件当正文
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fa import config

# `^` 不带 MULTILINE 时只锚定位置 0，正好表达「必须是文件第一行」。
_FRONTMATTER = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)

# 第三级认这两个目录名，和 Claude Code 一致。
RESOURCE_DIRS = ("references", "scripts")


def _read(path: Path) -> str:
    """读文本。

    一律 `errors="replace"`：SKILL.md 可能是模型自己写的，也可能来自别处，
    编码坏了不该让整轮对话炸掉 —— 退化成几个问号，比抛异常强。
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _is_anchored(path: Path) -> bool:
    """这个路径是不是「从某处根开始」的，而不是相对当前目录的。

    为什么不能只问 `is_absolute()`：在 Windows 上 `Path("/etc/passwd")` 的
    `is_absolute()` 是 **False** —— 它只有根、没有盘符。拼到 skill 目录上会
    变成 `C:\\etc\\passwd`，最终仍然会被 resolve 之后的边界判定拦住，但报出来
    的错会是「跑到目录外面去了」，对用户属于答非所问。

    `.root` 抓 `/etc/passwd` 这类，`.drive` 抓 `C:foo` 和 UNC 路径。
    """
    return path.is_absolute() or bool(path.drive) or bool(path.root)


@dataclass
class Skill:
    """一个 skill。

    正文**懒加载**：`discover()` 构造它时不读，第一次调 `body()` 才读。
    不过要注意，`discover()` 为了拿 frontmatter 本身就把文件读了一遍 ——
    「三级加载」说的是**进上下文**的量，不是磁盘读的次数。这两件事很容易
    混，但混了就会得出「discover 不读盘」的错误结论。
    """

    name: str
    description: str
    path: Path

    _body: str | None = field(default=None, repr=False)

    @property
    def dir(self) -> Path:
        return self.path.parent

    def body(self) -> str:
        if self._body is None:
            self._body = split_frontmatter(_read(self.path))[1].strip()
        return self._body

    def resources(self) -> list[str]:
        """第三级有哪些文件。返回相对 skill 目录的 posix 路径。"""
        found: list[str] = []
        for dirname in RESOURCE_DIRS:
            base = self.dir / dirname
            if not base.is_dir():
                continue
            for item in sorted(base.rglob("*")):
                if item.is_file():
                    found.append(item.relative_to(self.dir).as_posix())
        return found

    def read_resource(self, raw: str) -> str:
        """读一个第三级文件。

        这是唯一一条从 skill 名字出发、可能读到任意文件的路径，所以边界判定
        照搬 `workspace.safe_path`：**先 resolve 再判断**。只查字符串开头的话，
        `../../` 和指向外面的符号链接都能绕出去。
        """
        candidate = Path(raw.strip())
        if _is_anchored(candidate):
            return f"错误：path 要相对 skill 目录，收到了绝对路径 {raw!r}。"

        base = self.dir.resolve()
        target = (self.dir / candidate).resolve()
        if target != base and base not in target.parents:
            return f"错误：{raw!r} 跑到 {self.name} 的目录外面去了。"

        if not target.is_file():
            listed = self.resources()
            hint = "、".join(listed) if listed else "（这个 skill 没有附带文件）"
            return f"没有 {raw!r} 这个文件。可读的是：{hint}"

        text = _read(target)
        if len(text) > config.MAX_RESOURCE:
            return (
                text[: config.MAX_RESOURCE]
                + f"\n…[已截断，原文共 {len(text)} 字符]"
            )
        return text


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 (frontmatter 字典, 正文)。

    没有合法 frontmatter 时返回空字典 + 整份原文 —— 这正是文档说的
    「否则它把整份文件、包括 `---` 标记，都当作正文」。
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        # 坏掉的 YAML 不该让这个 skill 整个消失。退化成「没有 frontmatter」，
        # 至少正文还能用。
        return {}, text

    if not isinstance(meta, dict):
        return {}, text
    return meta, match.group(2)


def _describe(meta: dict, body: str) -> str:
    """描述。这是**唯一**影响模型选不选这个 skill 的东西。

    只认 `description`。这里以前还会拼一个自定义的 `when_to_use`，删掉是因为
    它只对自家的 agent 生效 —— Claude Code 不认识那个字段，skill 一旦共享过去
    就少了半句，而且不报错。触发信息写在 description 里，没有第二个地方。
    """
    return str(meta.get("description") or "").strip() or _first_line(body)


def _first_line(body: str) -> str:
    """正文里挑一句当描述。"""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return "(没有描述)"
    # 标题说的是「它叫什么」，下一行通常才是「它干什么」—— 模型是拿描述去
    # 判断「这个该不该用」的。整份文件只有标题时才退回去用标题。
    for line in lines:
        if not line.startswith("#"):
            return line
    return lines[0].strip("# ").strip()


def discover() -> list[Skill]:
    """扫描 skill 目录，取出名字和描述。

    **每轮重扫，不做缓存**：agent 刚写完的 SKILL.md 下一轮就能被自己用上，
    这个自举能力比省几次文件读值钱得多。

    目录从 config 模块**在调用时**读，而不是 `from fa.config import
    SKILLS_DIR` —— 后者是快照，别处 monkeypatch 就影响不到这里了。和
    `workspace.root()` 是同一个教训。
    """
    skills_dir = config.SKILLS_DIR
    if not skills_dir.is_dir():
        return []

    skills: list[Skill] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue

        meta, body = split_frontmatter(_read(skill_file))
        skills.append(
            Skill(
                name=str(meta.get("name") or entry.name).strip(),
                description=_describe(meta, body),
                path=skill_file,
            )
        )
    return skills


def find(skills: list[Skill], name: str) -> Skill | None:
    """按名字找。容忍模型把 `/name` 或 ` name ` 写成各种样子。"""
    wanted = name.strip().lstrip("/")
    for skill in skills:
        if skill.name == wanted:
            return skill
    return None
