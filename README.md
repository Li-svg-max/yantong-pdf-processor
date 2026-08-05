# 研通 PDF 题目裁剪服务

该服务处理 `privateMaterialApi` 投递的私人 PDF：下载原件、定位题号、按相邻题号纵向裁剪、上传题图，并把“题号 + 简要信息 + 原题图”回写到私人题库。

## 处理原则

- 有文字层的 PDF 直接读取文字坐标，不做 OCR；
- 扫描 PDF 只对页面左侧区域运行 RapidOCR，用于识别题号和短摘要；
- 公式和完整题干不转写，以 220 DPI 原图为准；
- 保持每页完整横向宽度，只改变题目图片的纵向边界；
- 跨页题目自动纵向拼接，超长图片自动在低墨迹行分片；
- SQLite 队列保存在 `/data`，容器重启后继续处理。

## 云托管部署

1. 在微信云开发控制台进入“云托管”，创建服务；
2. 构建目录选择 `services/pdf-processor`，使用目录内 `Dockerfile`；
3. 容器端口设置为 `8080`，实例数至少为 1；
4. 建议起步配置为 2 核 CPU、4GB 内存；
5. 挂载持久磁盘到 `/data`，否则容器重建时本地任务队列会丢失；
6. 部署后记录 HTTPS 服务地址，例如 `https://pdf-processor.example.com`。

容器环境变量：

```text
YANTONG_PROCESSOR_TOKEN=随机生成的长密钥
PRIVATE_MATERIAL_CALLBACK_URL=https://云开发HTTP访问域名/private-material-callback
COS_BUCKET=云存储对应的COS桶名称
COS_REGION=云存储区域，例如ap-shanghai
CLOUD_FILE_HOST=cloud文件ID中cloud://之后、第一个/之前的内容
TENCENT_SECRET_ID=仅具备该存储桶读写权限的密钥ID
TENCENT_SECRET_KEY=对应密钥
PROCESSOR_DATA_DIR=/data
MAX_PROCESS_ATTEMPTS=3
```

不要把腾讯云密钥或 `YANTONG_PROCESSOR_TOKEN` 写入小程序代码。COS 密钥应使用最小权限子账号，只允许读取 `private/*/imports/*` 和写入 `private/*/questions/*`。

## 回调配置

在云开发控制台的“HTTP 访问服务”中新增路由：

```text
路径：/private-material-callback
资源类型：云函数
目标：privateMaterialApi
```

然后给 `privateMaterialApi` 配置：

```text
YANTONG_PROCESSOR_TOKEN=与容器完全相同的密钥
YANTONG_PROCESSOR_URL=https://PDF处理服务域名
```

重新部署 `privateMaterialApi`。PDF 上传完成后，云函数会向 `YANTONG_PROCESSOR_URL/jobs` 投递任务。容器完成处理后调用 `PRIVATE_MATERIAL_CALLBACK_URL` 分批回写，每批最多 100 个题组。

在云函数配置中将 `privateMaterialApi` 的执行超时设为至少 10 秒。实际任务只在容器中运行，云函数只负责数据库写入和不超过 1.2 秒的任务投递。

## 验证

健康检查：

```powershell
Invoke-RestMethod "https://PDF处理服务域名/health"
```

应返回：

```json
{
  "ok": true,
  "service": "yantong-pdf-processor",
  "queue": {},
  "missing": []
}
```

如果 `ok` 为 `false`，`missing` 会列出尚未配置的环境变量名称，但不会返回任何密钥内容。

随后在小程序上传一份 PDF。处理记录应依次出现“已提交裁剪”和“可选题”。失败三次后会显示“裁剪失败”和具体原因，可点击“重试”重新投递原文件。

## 本地测试

```powershell
$env:PYTHONPATH="services/pdf-processor"
python -m unittest discover -s "services/pdf-processor/tests" -v
```

测试产物写入 `tmp/pdfs/pdf-processor-tests/`，包括文字层 PDF、纯图片 PDF、题目裁剪图和视觉检查联系表。

## 当前边界

当前服务处理题目 PDF，不自动把另一份答案册和题目逐题关联。答案册需要作为后续独立任务增加“答案题号检测 + `solutionImages` 回写”。
