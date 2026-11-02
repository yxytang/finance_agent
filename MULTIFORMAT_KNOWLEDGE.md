# 多格式知识入库

在页面侧边栏点击「知识库 · 导入资料」，选择文件后点击「解析并入库」。成功后可在聊天中提问，例如“请查阅我上传的资料，说明预算安排，并注明文档页码”。知识库列表支持下载原件。

Web 上传立即返回 HTTP 202 和任务编号；页面轮询显示排队、解析、等待重试或完成状态，支持取消当前任务。关闭或刷新页面不会丢弃已提交任务，再次打开知识库会恢复本浏览器记录的任务状态。解析与 OCR 在后台 worker 中执行，不占用上传请求。运行中取消是协作式的，原件已提交时会如实显示成功；当前解析不能强行杀死。队列、重试和独立 worker 配置见 [BACKEND.md](BACKEND.md)。

| 文件类型 | 提取内容 | 来源定位 |
|---|---|---|
| Markdown | 标题层级与正文 | 文件、标题路径 |
| PDF | 原生文字；图片页、图文混排页本地OCR | 文件、第几页 |
| PPTX | 文本框、组合形状、表格、演讲者备注、内嵌图片OCR | 文件、第几张幻灯片、标题及内容类型 |
| PNG/JPG/JPEG/WEBP/BMP/TIF/TIFF | 本地中英文OCR；多页TIFF按帧处理 | 文件、图片页号 |
| 旧版PPT | 经LibreOffice转为PPTX后解析 | 原PPT文件、幻灯片位置 |

这是**多格式文本入库**：图片与扫描页经过文字识别后参与现有BM25／向量检索。当前不提供图像向量召回、图表走势理解或凭图片生成解释；没有可识别文字的图片会明确报错。OCR金额、日期、列结构可能识别有误，需要对照原件核对，不能直接作为账单数值真值。

## 安装与运行

```powershell
pip install -e ".[web,documents]"
python -m web.server --port 8765
```

OCR使用RapidOCR ONNX Runtime，中英文识别模型随依赖包安装。本地解析不调用云端模型、不需要额外视觉API密钥；后续向量入库和模型问答仍使用现有RAG及模型配置。

旧版`.ppt`需要额外安装LibreOffice并将`soffice`加入PATH。未安装时会提示另存为`.pptx`，不会把不能解析的文件当作入库成功。当前提取的是嵌入文档内的文字和图片。PPT图表若未包含可提取文本或位图，不能保证提取其完整图表数据。

也可以使用命令行批量导入：

```powershell
python -m fa.knowledge "D:\资料\理财手册.pdf" "D:\资料\培训.pptx" "D:\资料\说明.png"
# 扫描已有knowledge目录并更新索引
python -m fa.knowledge --rebuild
# 指定另一个知识目录
python -m fa.knowledge "D:\资料\手册.pdf" --root "D:\我的知识库"
```

每个文件最多20MB，文档或多页图片最多100页，单页图片最多2000万像素。超限、损坏、加密、缺少依赖及未识别到文字均给出错误。文件同名时不覆盖，请重命名后导入。批次中有失败会返回非零退出码，其他已成功导入的文件仍保留。

## 索引与缓存

原文件保留在`knowledge/uploads/`，解析缓存保留在`knowledge/.index/extracted/`，两者均被Git忽略。上传会在临时目录解析，成功后原子发布原件并更新词法索引；解析失败不会留下一个伪成功的知识文件。向量索引在后续检索时按现有流程增量加载，页面上传成功不表示已完成远程embedding调用。

解析缓存以**原文件内容、格式、解析方案、依赖版本、OCR模式**为键。原件内容、页序或解析配置变化会使索引失效；缓存命中时不会重复OCR。删除知识原件后，下一次检索会重建索引并去除其结果。用户可以从本地上传目录管理资料，当前界面不提供删除或覆盖入口。

`Chunk`增加可选`page`、`slide`、`extraction_method`，保存到索引并随结果保留，文本引用同时带可读页码。原有MD分块和纯MD语料指纹保持不变；历史评测题面不会被重写。

可配置环境变量`KNOWLEDGE_OCR_MODE`：

- `auto`（默认）：图片OCR；PDF无文字或包含位图时OCR；PPT内嵌图片OCR。
- `always`：PDF所有页均OCR并合并原生文字。
- `off`：只提取原生文字；独立图片和需要OCR的PDF页会提示错误；PPT内嵌图片跳过。

命令行也接受`--ocr auto|always|off`，它只影响当前进程。网页与命令行若使用不同配置，下一次检索会按网页进程的配置重新解析。

## 验证

`tests/test_documents.py`运行时生成PDF、PPTX与图片，覆盖原生文字、图文混排、扫描OCR、PPT组合文字/表格/备注/图片、来源持久化、内容及配置变更失效、失败回滚、同名保护、鉴权和原件下载边界。测试数据不包含真实用户账单。CI安装documents extra，运行同一组测试。

解析实现参考：[PyMuPDF页面API](https://pymupdf.readthedocs.io/en/latest/page.html)、[python-pptx形状API](https://python-pptx.readthedocs.io/en/latest/api/shapes.html)、[RapidOCR项目](https://github.com/RapidAI/RapidOCR)。
