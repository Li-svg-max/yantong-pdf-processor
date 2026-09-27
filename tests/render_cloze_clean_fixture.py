"""Layout-only samples for the minimal Word template, not user documents."""
import base64
from pathlib import Path
from app.document_export.models import ClozeExportRequest
from app.document_export.renderer import render_cloze_export

out = Path(__file__).resolve().parents[3] / 'output' / 'cloze-clean-word-20260919'
out.mkdir(parents=True, exist_ok=True)
for family, size, show_title in [('SimSun', 12, True), ('KaiTi', 18, False)]:
    text = '学习资料应保留原文顺序和段落。重点词确认后才进入练习。\n核对时，先看定义，再检查使用条件。'
    blocks = [dict(id=f'b{i}', order=i + 1, text=text,
                   blanks=[dict(id=f'k{i}', start=0, end=4, answer='学习资料')]) for i in range(6)]
    req = ClozeExportRequest(title='学习方法练习', courseName='测试', format='docx', variant='combined',
                             wordStyle=dict(fontFamily=family, fontSize=size, showTitle=show_title), blocks=blocks)
    file = render_cloze_export(req)[0]
    target = out / f'clean-{family}-{size}.docx'
    target.write_bytes(base64.b64decode(file['contentBase64']))
    print(target)
