# 检索、路由与回答评测

新增100题覆盖字面30、转述30、多证据15、知识库无法回答15、工具路由10题，保留旧20题检索基线和21题问答集用于历史对照。新题是**助手起草、待人工复核**，不能写成“100题人工标注集”。逐题问题、参考要点、原文锚点及工具要求见 [benchmark_review.md](benchmark_review.md)，可编辑数据定义见 [benchmark_cases.py](benchmark_cases.py)。

## 题库边界与划分

开发集60题、测试集40题。字面及转述题按题族分组，多证据及混合路由题继承对应证据的划分；校验拒绝同一题族或同一证据小节跨划分。测试集用于方案确定后的验收，暂未执行测试集实验。

这是同一12篇文档内的**问题与证据小节隔离**，不代表跨文档或跨领域泛化。无答案是相对现有知识库而言，检索到邻近主题不代表有答案。知识库中的政策金额、保险及贷款规则是文档快照，本评测不验证现行法规，也不自动更新原文。人工复核还需补充可替代证据，避免正确的其他来源被误判为漏检。

复核时在数据定义中逐题调整参考答案、证据、工具槽位及 `annotation_status`，复核后再生成清单。单纯运行校验或勾选Markdown不能自动把题目变成人工金标准。旧题和新题口径不同，不能把两者分数拼成同一条提升曲线。

## 离线检索基线

在仓库根目录运行，以下命令不需要API：

```powershell
python -m eval.benchmark --review eval/benchmark_review.md
python -m eval.benchmark --split dev --repeat 3 --output eval/runs/bm25-dev.json
python -m eval.benchmark --split dev --repeat 3 --lexical title --output eval/runs/title-dev.json
python -m eval.benchmark --split dev --repeat 3 --lexical body --output eval/runs/body-dev.json
```

默认模式为生产分词、分块下的标题与正文两路BM25，经RRF融合。所有模式候选最多20个父块，最终返回5个，MRR只看前10。指标包括：

| 指标 | 口径 |
|---|---|
| Hit@1/@3/@5 | 前k个结果至少包含一个正确证据单元 |
| MRR@10 | 第一个正确证据的倒数排名，前10未命中记0 |
| candidate_recall@20 | 候选集覆盖必需证据单元的比例 |
| evidence_recall@5 | 最终前5个结果覆盖必需证据单元的比例 |
| p50/p95 | 所有尝试的检索加查询改写耗时，包含失败；初始化单独报告，成功请求延迟另列 |

证据按**来源文件+小节+原文锚点**匹配。同名小节不会串文档，重复返回同一证据不会提高Recall。多证据题的Hit只要求命中任意一项，判断完整性必须看evidence_recall。无答案题输出检索记录，但不参与正向检索分母；路由题不走直接检索实验。

JSON记录配置、语料/题库/脚本指纹、模型、后端、逐题候选和最终正文、耗时及调用量；同名Markdown记录分组成绩和漏检题。服务失败保留在计划分母，同时给出成功样本均值和失败按0计的操作成绩。任意题执行失败，进程返回非零。

现已使用真实text-embedding-v4及gte-rerank-v2完成开发集消融，见
[真实API对照结果](benchmark_online.md)。主实验为显式内存余弦后端，不能当作Chroma实测。
新题仍待人工复核，测试集未执行；RAGAS judge、multi-query及HyDE实测尚未完成。

**默认不使用本地检索缓存**，每次重建实验索引；重复3次不等于3倍独立题目，词法排名本来就是确定的。延迟是小语料本机测量，不能据此宣称并发吞吐、用户首字延迟或线上p95。调用计数是客户端方法及输入量，不等于底层HTTP重试次数或实际费用。

## 向量、重排及查询改写消融

在 `.env` 配置RAG_API_KEY后，以下命令会产生真实API调用：

```powershell
# 显式内存余弦后端，真实embedding；四种方案使用同一题面
python -m eval.benchmark --modes bm25 dense hybrid rerank --split dev --repeat 3 --output eval/runs/ablation-child.json
# 父块embedding对照，不修改生产分块
python -m eval.benchmark --modes dense hybrid rerank --embedding-unit parent --split dev --output eval/runs/ablation-parent.json
# Chroma必须安装成功；失败直接报错，不改用内存库
python -m eval.benchmark --modes hybrid rerank --store chroma --split dev --output eval/runs/ablation-chroma.json
# 需要DEEPSEEK_API_KEY；改写仅进入实验脚本
python -m eval.benchmark --modes hybrid rerank --rewrite multi --split dev --output eval/runs/multi.json
python -m eval.benchmark --modes hybrid rerank --rewrite hyde --split dev --output eval/runs/hyde.json
```

四种方案分别为词法两路、纯向量、三路RRF、三路RRF后重排。重排默认模型以实际配置为准，仓库默认为gte-rerank-v2；不能未切换模型便写成qwen3-rerank。

实验向量路在当前小语料上取全部子块后去重并截取父块，避免重复子块占满候选；这是与生产DenseIndex的“先取k个子块再去重”不同的实验设置，报告显式记录，不能当作原生产链路成绩。实验Chroma位于eval/runs/chroma，与生产索引隔离。语料初始化每次重做，不能按此测量线上索引复用性能。

多查询扩展保留原问题再融合3个改写问句；HyDE保留原问题和假设文档。每题每次重复只生成一次改写，供各模式共享以保证比较输入一致，改写耗时及token用量记录在逐题结果。假设文档只参与召回，不作为回答证据。改写、embedding、向量库或重排失败均不会悄悄改用BM25。

调参入口包括 `--lexical title|body|both`、`--rrf-k`、`--per-channel`。只在dev调参，最终确定配置后再运行一次test。已有开发集对照见 [benchmark_baseline.md](benchmark_baseline.md)，不能把dev效果称为测试集泛化提升。

## Agent路由及回答

```powershell
# 默认仅检查题目清单，不发请求
python -m eval.benchmark_agent --split dev --limit 3
# 显式执行真实模型调用，每题新会话；可先少量冒烟
python -m eval.benchmark_agent --run --split dev --limit 3 --output eval/runs/agent-smoke.json
python -m eval.benchmark_agent --run --group tool_routing --split dev --repeat 3 --output eval/runs/routes.json
python -m eval.benchmark_agent --run --mode rerank --split dev --repeat 3 --output eval/runs/agent-rerank.json
```

采集使用Session原有循环，但固定为直接只读工具配置，不加载个人记忆、技能、MCP及子agent，因此不是完整生产Agent的端到端成绩。此配置让工具调用可直接观察，避免写入记忆或改动账单。

每个工具槽位需要至少调用一个允许工具；混合题必须覆盖账单与知识两个槽位。`required_tool_recall`测调用覆盖，`successful_tool_recall`还要求执行成功，`route_correct`要求全部槽位并未调用禁止工具。无答案题允许“查后说明不足”或“直接说明不足”，不机械要求调用工具，拒答是否正确由人工核对。

记录实际送入模型的最终知识块、全部检索轮次、工具调用及结果、token用量和总耗时。逐题落盘，报告保留批次计划数与完成数。失败不重试成成功，重复运行分别记录。账单日期、数据文件指纹同样进入报告；具体金额必须以工具结果为准。

输出 `.review.json` 留待人工填写：`reviewer`、`key_point_coverage`、`evidence_supported`、无答案题的 `correct_abstention` 和备注。分数为0到1，未复核保留null。汇总不会把空白变成成功：

```powershell
python -m eval.manual_review eval/runs/agent-smoke.review.json
```

RAGAS只接收成功执行的正向知识题，账单和拒答题单独评；导出数量与原始计划数必须一起披露：

```powershell
python -m eval.ragas_run --input eval/runs/agent-smoke.ragas.json --dry
python -m eval.ragas_run --input eval/runs/agent-smoke.ragas.json --output eval/runs/ragas-smoke.md
```

需安装 `.[eval]`。自定义输入不会覆盖历史findings.md。报告展示各指标有效分母，judge返回NaN不能悄悄忽略成一份“全部成功”的结果。faithfulness表示回答与检索上下文的一致性，不能代替事实正确率；参考答案覆盖、证据支持与拒答应结合人工复核。当前没有完成新题的RAGAS实测，不提供虚构分数。

## 简历新增条目

完成当前工程后可写：

> 检索评测：构建覆盖字面、转述、多证据、无答案及工具路由的100题评测草案，按题族与证据小节划分开发/测试集；实现候选证据Recall@20、Hit@K、MRR@10、最终证据Recall@5及逐题错误分析，支持BM25、向量、RRF融合、重排及查询改写消融，记录模型配置、数据指纹、调用量与p50/p95，严格区分服务失败与检索漏检。

待逐题人工核对后，才能将“评测草案”改成“人工标注评测集”；待真实向量、重排、RAGAS及测试集实验完成后，再补具体提升及成本数字。父子块、查询改写是否提升质量都要由对应实验说明。
