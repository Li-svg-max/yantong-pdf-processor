# 公式 OCR 服务

该服务只处理一张已裁剪的公式图片，并返回 LaTeX 与可编辑表达式。它与 PDF 裁剪服务独立部署。

本版本直接使用 `breezedeus/pix2text-mfr-1.5` 的 ONNX 公式识别模型，不安装 Pix2Text 的文字 OCR、版面分析、PDF 和图像检测组件。这样能避免把无关依赖打进镜像，降低云托管的镜像拉取、启动和内存压力。

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
  "release": "formula-only-onnx-v1",
  "modelLoaded": false,
  "modelLoading": false,
  "modelError": ""
}
```

服务创建与健康检查不加载模型。只有调用 `POST /warmup` 或首次调用 `POST /recognize` 时才会下载并加载公式模型，因此第一次预热需要较长时间。预热期间反复查询 `/health`；只有 `modelLoaded` 为 `true` 后，再从小程序发起图片识别。

模型输出仅用于辅助录入，用户仍须在公式编辑器中确认后再绘图。不要把模型输出当作数学符号完全正确的保证。

## 本地验证

```powershell
python -m unittest discover -s tests -v
```
