"""Create clearly labelled layout-only fixtures, not extracted course material.

Run only after the artifact-operation marker has been successfully coordinated.
The final PDFs must still be rendered and inspected before being shared.
"""
from __future__ import annotations

import argparse
import base64
from pathlib import Path

from app.document_export.models import ClozeExportRequest
from app.document_export.renderer import render_cloze_export


PARAGRAPHS = [
    ("整理一份学习资料时，可以先写下学习目标，再辨认材料中的关键概念。阅读结束后，用自己的语言记录理解，而不是只抄写整段文字。把尚未理解的地方放进复习记录，下次学习时再逐项核对。", ["学习目标", "关键概念", "复习记录"]),
    ("为了方便查找，可以给每一段材料设置段落标题，并保留原来的叙述顺序。遇到相近但不同的术语，应分别写出使用条件。核对时先比较定义，再检查例子中的具体情境，不要因为字面相近就合并两个概念。", ["段落标题", "叙述顺序", "使用条件"]),
    ("填写练习之前，先通读一句话的完整语境，观察空格前后的限定词。答完后不仅要核对词语，还要说明这个词与上下文的联系。如果没有把握，可以在补充栏留下疑问标记，之后再回到材料中检查。", ["完整语境", "限定词", "疑问标记"]),
    ("讨论一个观点时，可以区分事实描述与个人解释。前者应能在材料中找到明确依据，后者则需要说明推理过程。暂时不能确定的内容应保留不确定性，不应为了使句子简短而删掉条件或把可能的情况写成必然结果。", ["事实描述", "个人解释", "不确定性"]),
    ("做完一次练习后，可以先进行独立回忆，再打开答案核对。把遗漏的关键词与理解错误分开记录，有助于判断下一次应侧重记忆还是解释。下一轮练习不必照搬同样的顺序，可以调整提问方式，检查能否在不同表述中辨认同一概念。", ["独立回忆", "关键词", "提问方式"]),
    ("处理较长的段落时，可以先确认中心意思，再逐句检查各个分句之间的关系。遇到表示转折、条件或因果的连接词，应保留它们所在的句子。摘要可以缩短篇幅，但不应改变原文中明确给出的条件范围。", ["中心意思", "连接词", "条件范围"]),
    ("为打印材料安排版面时，应保证正文清楚、编号稳定，并留出足够的答题空间。较长内容可以自然分页，答案表格跨页时应重复列名。页脚中的页码帮助读者确认顺序，但不应压住正文或占用过多的写字位置。", ["答题空间", "自然分页", "页码"]),
    ("整理错题记录时，可以把问题原文、自己的实际作答和后续说明分别保存。只问过一道题，并不等于已经做错；表示听懂了，也不等于已经能够独立完成。需要进一步判断时，应提供新的练习机会，并保留实际表现作为核对依据。", ["问题原文", "实际作答", "核对依据"]),
    ("复习安排可以围绕当前最需要解决的困难展开。先确定一个具体问题，再选择与它相关的材料，最后检查是否获得了可解释的进展。如果只记下笼统的目标，就很难知道下一次从哪里继续；因此记录应尽量写明下一步动作。", ["具体问题", "可解释的进展", "下一步动作"]),
    ("分享一份整理后的练习时，应确认资料授权与使用范围，并避免夹带账号信息或私人备注。练习版与答案版需要明确区分，发送前还应检查文件名称及实际内容是否一致。本文仅用于排版功能测试，不对应任何网盘教材或课程讲义。", ["资料授权", "使用范围", "私人备注"]),
]


def fixture_request(variant, format_name):
    blocks = []
    for index, (text, answers) in enumerate(PARAGRAPHS, start=1):
        blanks = []
        for number, answer in enumerate(answers, start=1):
            start = text.index(answer)
            blanks.append({"id": f"test-{index}-{number}", "start": start, "end": start + len(answer), "answer": answer})
        blocks.append({"id": f"test-block-{index}", "order": index, "page": (index - 1) // 5 + 1, "text": text, "blanks": blanks})
    return ClozeExportRequest(title="排版测试资料 非网盘提取", courseName="自编中文功能测试", variant=variant, format=format_name, blocks=blocks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    target = Path(args.output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    for format_name, variants in (("pdf", ("practice", "answers", "combined")), ("docx", ("combined",))):
        for variant in variants:
            file = render_cloze_export(fixture_request(variant, format_name))[0]
            path = target / f"cloze-layout-test-{variant}.{format_name}"
            path.write_bytes(base64.b64decode(file["contentBase64"]))
            print(path)


if __name__ == "__main__":
    main()
