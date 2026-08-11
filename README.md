# 公式 OCR 独立服务

该服务只负责把单张公式图片识别为 LaTeX，数学工具页随后将结果放入可编辑公式框。它与 PDF 裁剪服务分开部署，避免模型运行时拖慢题号检测和裁剪任务。

## 部署边界

- 云托管服务名必须为 `formula-ocr`，容器端口 `8080`；
- CPU 方案建议至少 4 核 8GB，单实例并发设为 1，并保留最少 1 个实例以避免首次加载超时；
- 模型会在容器启动阶段下载并加载，首次发布可能需要数分钟；服务稳定后再发布新版本，避免在模型加载期间切流；
- 当前接口限制单图 4MB、2000 万像素，小程序会先压缩再传输；
- 返回内容必须由用户在公式编辑器中确认后再绘图，不能承诺数学符号 100% 正确。

## CloudBase 发布参数

- 代码仓库：`Li-svg-max/yantong-pdf-processor`
- 分支：`formula-ocr`
- 构建目录：仓库根目录
- Dockerfile：`Dockerfile`
- 监听端口：`8080`
- 最小实例数：`1`
- 最大实例数：`1`
- 单实例并发：`1`
- 健康检查路径：`/__tcb_probe__`

该分支与 `main` 中的 PDF 处理服务完全隔离，不会触发 `pdf-processor` 的源码发布。创建云托管服务时必须使用服务名 `formula-ocr`，否则小程序的 `wx.cloud.callContainer` 无法路由到该容器。

发布完成后，`GET /health` 应返回 `ok: true`、`modelLoaded: true` 和 `service: yantong-formula-ocr`。若 `modelLoaded` 不是 `true`，不要开始图片识别测试。

构建日志中不应出现 `nvidia-cuda-*`、`nvidia-cudnn-*` 或 `nvidia-nccl-*`。这些依赖表示 pip 错误安装了 GPU 版 PyTorch，会把镜像膨胀到约 10GB 并导致 CloudBase 推送超时。当前 Dockerfile 通过固定 URL 和 `constraints.txt` 安装 CPU 轮子；正常镜像不应包含 CUDA 运行库。固定 wheel 下载阶段会输出进度，避免构建平台因长时间无日志而中止。

部署前必须分别核对 Pix2Text 代码、检测模型、公式识别模型及底层 OCR 组件的许可证和商用条件。模型输出、用户确认结果和原图应分开保存；当前版本不持久化公式图片。

本地测试（不加载模型）：

```powershell
python -m unittest discover -s tests -v
```
