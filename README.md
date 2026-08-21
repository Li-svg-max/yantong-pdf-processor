# 研通 PDF 题目裁剪服务

该服务处理 `privateMaterialApi` 投递的私人 PDF：下载原件、定位题号、按相邻题号纵向裁剪、全文 OCR、保留内部裁剪件，并把“题号 + OCR 文字”回写到私人题库。服务同时提供小程序排版页使用的 PDF/Word 文档导出接口。

## 处理原则

- 有文字层的 PDF 直接读取文字坐标，不做 OCR；
- 扫描 PDF 先对页面左侧区域运行 RapidOCR 定位题号，再对每个完整裁剪区域执行全文 OCR；
- 全文 OCR 会比较原彩图、增强灰度图和抑制蓝绿手写笔迹后的图，选择较优结果；
- OCR 文字用于题目总览、详情、搜索、排版和知识点初筛，低置信度或有墨迹风险时标记为待核对；
- 裁剪件仅保留在后台用于重新识别、质量追溯和安全删除，不会在小程序详情、排版页或导出文件中展示；
- 保持每页完整横向宽度，只改变题目图片的纵向边界；
- 跨页题目自动纵向拼接，超长图片自动在低墨迹行分片；
- SQLite 队列保存在 `/data`，容器重启后继续处理。

## 文档导出

`POST /export` 接收小程序排版设置与题组，生成真实 PDF 或 DOCX。支持 A4/A5、单栏/双栏、字号、题间距、答题留白、答案行内/文末，以及试题和答案解析分成两个文件。导出始终使用题目文字，不嵌入扫描原图或裁剪件。

专业课资料使用独立流程：文字 PDF 直接提取全文，扫描 PDF 或照片使用 RapidOCR 识别整页文字，不要求存在题号。机器只生成保守的挖空建议，用户可以修改识别文字、关闭或删除建议、修改答案和增加自定义挖空。`POST /cloze-export` 生成先练习后答案的 DOCX，答案区包含编号和原文定位。

文档导出复用同一个 `pdf-processor` 服务，不需要新增云托管服务。两类导出都必须携带 `privateMaterialApi` 签发的 5 分钟 HMAC 票据；专业课票据只从当前用户已核对保存的私人文档生成，客户端不能绕过核对直接替换内容。生成后文件写入小程序用户文件目录，并使用系统文档查看器打开。

## 云托管部署

1. 在微信云开发控制台进入“云托管”，创建服务；
2. 构建目录选择 `services/pdf-processor`，使用目录内 `Dockerfile`；
3. 容器端口设置为 `8080`，实例数至少为 1；
4. 建议起步配置为 2 核 CPU、4GB 内存；
5. 挂载持久磁盘到 `/data`，否则容器重建时本地任务队列会丢失；
6. 服务名称必须使用 `pdf-processor`，小程序通过云托管原生 `callContainer` 调用，不需要公网服务地址。

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
QUEUE_POLL_INTERVAL=1
MAX_CONCURRENT_EXPORTS=2
```

`MAX_CONCURRENT_EXPORTS` 建议保持为 `2`，允许两份文档并行生成，同时控制 2 核 4GB 实例的内存压力。不要把腾讯云密钥或 `YANTONG_PROCESSOR_TOKEN` 写入小程序代码。COS 密钥应使用最小权限子账号，只允许读取 `private/*/imports/*` 和写入 `private/*/questions/*`。

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
```

重新部署 `privateMaterialApi`。PDF/照片上传完成后，云函数生成 5 分钟有效的 HMAC 签名任务票据，小程序通过 `wx.cloud.callContainer` 向 `pdf-processor/cloudbase/jobs` 或 `cloudbase/image-jobs` 投递。生成 PDF/Word 时也会使用短时票据，永久密钥不会进入小程序。容器完成处理后调用 `PRIVATE_MATERIAL_CALLBACK_URL` 回写私人题组或私人专业课文档。

在云函数配置中将 `privateMaterialApi` 的执行超时设为至少 10 秒。实际任务只在容器中运行，云函数只负责数据库写入和不超过 1.2 秒的任务投递。

## 验证

云托管版本的容器健康检查应显示正常。若另行配置了无需 CloudBase access token 的公网域名，也可以执行：

```powershell
Invoke-RestMethod "https://PDF处理服务公网域名/health"
```

应返回：

```json
{
  "ok": true,
  "service": "yantong-pdf-processor",
  "documentExportFormats": ["pdf", "docx"],
  "queue": {},
  "missing": []
}
```

如果 `ok` 为 `false`，`missing` 会列出尚未配置的环境变量名称，但不会返回任何密钥内容。

公网健康地址不是原生调用的必需条件。随后在小程序上传一份 PDF，处理记录应依次出现“已提交文字识别”和“可选题”。失败三次后会显示“文字识别失败”和具体原因，可点击“重试”重新生成票据并投递原文件。

## 本地测试

```powershell
$env:PYTHONPATH="services/pdf-processor"
python -m unittest discover -s "services/pdf-processor/tests" -v
```

测试产物写入 `tmp/pdfs/pdf-processor-tests/`，包括文字层 PDF、纯图片 PDF、题目裁剪图和视觉检查联系表。

## 当前边界

当前服务不会自动从被黑色墨迹完全遮挡的区域恢复原始印刷文字；这类题会保留原图并标记人工核对。服务也不自动把另一份答案册和题目逐题关联，答案册需要后续增加独立的题号匹配任务。
