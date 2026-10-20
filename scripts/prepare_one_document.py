import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

source = ROOT / "data/corpus/raw/kubernetes/debug-pods.md"
target = ROOT / "data/corpus/processed/kubernetes/debug-pods.md"

raw = source.read_text(encoding="utf-8-sig")

# 提取文件开头的 YAML 元信息，保留文档标题。
frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", raw, re.S)
title = None
text = raw

if frontmatter:
    title_match = re.search(
        r"^title:\s*(.+)$", frontmatter.group(1), re.M
    )
    if title_match:
        title = title_match.group(1).strip().strip("'\"")
    text = raw[frontmatter.end():]

# 删除 HTML 注释，包括其中的英文原文。
text = re.sub(r"<!--.*?-->", "", text, flags=re.S)

# 删除标题末尾的网页锚点，保留 Markdown 标题。
text = re.sub(
    r"(?m)^(#{1,6}\s+.*?)\s+\{#[^}]+\}\s*$",
    r"\1",
    text,
)

# 将 Kubernetes 网站的标题模板转换为普通文本。
text = text.replace('{{% heading "whatsnext" %}}', "接下来")

# 压缩过多空行。
text = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", text).strip()

if title:
    text = f"# {title}\n\n{text}"

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text + "\n", encoding="utf-8")

print(f"原始字符数：{len(raw)}")
print(f"清洗后字符数：{len(text)}")
print(f"输出文件：{target}")
print(f"是否还有模板标记：{'{{' in text}")