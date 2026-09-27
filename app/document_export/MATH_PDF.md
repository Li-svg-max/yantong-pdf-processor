# PDF 公式排版边界

2026-09-15核查发现，原先 `_pdf_paragraph` 仅对文字做 HTML escape，`$\lim_{x\to0}\frac{\sin x}{x}$` 会在PDF中原样显示控制词。现在仅PDF路径使用本地 MathText，将公式排为300dpi透明PNG并直接以内存data插入ReportLab Paragraph，支持真正堆叠分式、上下标、根号、极限、积分等 MathText 子集。

版本依据：Matplotlib 官网稳定文档与PyPI的 `3.11.2`；此版本需要 Python>=3.11、NumPy>=1.25，与当前Docker Python3.11、requirements NumPy1.26.4相容。增加的是 `matplotlib==3.11.2`，不依赖TeX发行版或远程公式服务。部署服务器须重新构建/安装requirements；只更新小程序或云函数不会更新该Python服务。

- 官方API：https://matplotlib.org/stable/api/mathtext_api.html
- 官方语法范围：https://matplotlib.org/stable/users/explain/text/mathtext.html
- 固定版本元数据：https://pypi.org/pypi/matplotlib/3.11.2/json

安全边界：

- 仅识别成对 `$…$`、`$$…$$`、`\(…\)`、`\[…\]`，普通文字仍转义。
- 从不启用 `text.usetex`、执行用户TeX、读取用户给出的路径或用用户内容构造远程图片URL。MathText自身是有限语法排版器。
- 单公式1024字符、最多12层花括号；先矢量测量再分配像素，最多520×180pt并且不超过150万像素；单PNG128KB。
- 进程内LRU缓存最多64项（每项PNG最多128KB）；每段最多64公式，每份PDF最多512公式、公式PNG总量8MB。无持久公式缓存文件。
- ReportLab根据真实公式高度增加行高。公式超过当前栏宽则明确要求改单栏或拆分，不裁切、不默默缩到不可读字号。
- 不支持的命令、缺字警告或格式错误会拒绝生成该PDF，提示核对公式。不会去掉命令继续输出可能错误的数学含义。
- DOCX路径完全不变，不能据此声称Word公式同样已实现。
- 公式图片不是可搜索的数学文本；中文题干和普通文字仍为PDF文字。最终验收须查看实际PDF渲染，不能只看文本提取。

测试：`python -m unittest discover -s services/pdf-processor/tests -p test_math_pdf.py -v`（设置PYTHONPATH为 services/pdf-processor）。专项测试只检查内存PNG/段落布局，不创建最终PDF；最终生产打印样张由总验收流程生成并逐页检查。
