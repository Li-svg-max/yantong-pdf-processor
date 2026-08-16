# 公式 OCR 服务

该服务使用两阶段流程：先从整张题图中定位公式区域，再将用户选择的区域识别为 LaTeX 与可编辑表达式。它与 PDF 裁剪服务独立部署。

本版本使用 `breezedeus/pix2text-mfd-1.5` 公式区域检测模型和 `breezedeus/pix2text-mfr-1.5` ONNX 公式识别模型。它不会安装完整的文字 OCR、PDF 和版面分析链路，但会多出约 77MB 的检测模型和必要运行依赖。

## 云托管创建参数

- 服务名称：`formula-ocr`
- 代码仓库：`Li-svg-max/yantong-pdf-processor`
- 分支：`formula-ocr`
- 构建目录：仓库根目录
- Dockerfile：`Dockerfile`
- 监听端口：`8080`
- 健康检查路径：`/__tcb_probe__`
- 最小实例数：`1`
- 最大实例数：`1`
- 单实例并发：`1`
- 建议规格：先使用 `4 核 8GB` CPU；模型成功加载并完成一轮识别测试后，再按实际内存占用下调。

创建成功后，先通过 `GET /health` 查看服务状态。首次返回应包含：

```json
{
  "ok": true,
  "service": "yantong-formula-ocr",
  "release": "mfd-mfr-onnx-v3",
  "modelLoaded": false,
  "detectorLoaded": false,
  "modelLoading": false,
  "modelError": ""
}
```

模型文件在 Docker 构建阶段下载并固化到 `/opt/formula-model` 和 `/opt/formula-detector`。服务运行时启用离线模式，不会再从 Hugging Face 下载文件。服务启动后默认只在后台预热公式识别模型，区域检测模型按需加载；调用 `POST /warmup` 仍然是幂等的补偿入口。小程序会轮询 `/health`，只有目标模型加载完成后才发起图片识别。已经裁剪到单条公式的图片会跳过区域检测模型，直接使用识别模型。

## 接口

- `POST /analyze`：返回按阅读顺序排列的公式候选框。
- `POST /recognize-region`：仅识别传入候选框，适合一张题图中有多个公式。
- `POST /recognize`：整图回退识别，仅适合已经裁剪为单条公式的图片。

模型输出仅用于辅助录入，用户仍须在公式编辑器中确认后再绘图。不要把模型输出当作数学符号完全正确的保证。

## 本地验证

```powershell
python -m unittest discover -s tests -v
```
