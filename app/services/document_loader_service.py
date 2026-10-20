"""加载已清洗的 Markdown 语料；不切分、不调用模型、不连接向量库。"""

import hashlib
import logging
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from langchain_core.documents import Document
from langchain_community.document_loaders import TextLoader
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError
from langsmith import traceable

logger = logging.getLogger(__name__)


class DocumentLoadError(RuntimeError):
    """文档或来源配置不符合加载要求。"""


class _SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    language: str = Field(min_length=1)


class _DocumentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: HttpUrl


class _CorpusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    default_document: str = Field(min_length=1)
    sources: dict[str, _SourceConfig]
    documents: dict[str, _DocumentConfig] = Field(default_factory=dict)


class DocumentLoaderService:
    """按相对语料路径返回一个完整的 LangChain Document。

    目录约定：corpus_root/{raw,processed}/产品名/文档.md。
    metadata 中的路径均相对 corpus_root，使用正斜杠以便跨平台保存。
    """

    @classmethod
    @traceable(name="documents.load", run_type="chain")
    def load_file(cls, file_path: str | Path) -> Document:
        """正式索引入口：加载上传的 Markdown/文本，并复用语料来源记录。"""
        path = Path(file_path).resolve()
        if path.suffix.lower() not in {".md", ".txt"}:
            raise DocumentLoadError("仅支持 .md 和 .txt 文件")
        corpus_root = Path(__file__).resolve().parents[2] / "data" / "corpus"
        processed_root = corpus_root / "processed"
        if path.is_relative_to(processed_root) and (corpus_root / "sources.toml").is_file():
            document = cls(corpus_root).load(path.relative_to(processed_root).as_posix())
        else:
            document = TextLoader(str(path), encoding="utf-8-sig").load()[0]
            if not document.page_content.strip():
                raise DocumentLoadError(f"文档正文为空：{path}")
            title = path.stem
            if path.suffix.lower() == ".md":
                try:
                    title = cls._extract_title(document.page_content)
                except DocumentLoadError:
                    pass
            document.metadata.update({
                "title": title,
                "document_id": hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest(),
                "content_sha256": hashlib.sha256(
                    document.page_content.encode("utf-8")
                ).hexdigest(),
            })
        document.metadata.update({
            "_source": path.as_posix(),
            "_extension": path.suffix.lower(),
            "_file_name": path.name,
        })
        return document

    def __init__(self, corpus_root: Path, *, max_file_bytes: int = 10 * 1024 * 1024) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0")
        self.corpus_root = Path(corpus_root).resolve()
        self.max_file_bytes = max_file_bytes
        config_path = self.corpus_root / "sources.toml"
        try:
            with config_path.open("rb") as file:
                self._config = _CorpusConfig.model_validate(tomllib.load(file))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise DocumentLoadError(f"无法读取来源配置 {config_path}：{exc}") from exc

    def load(self, document_key: str | None = None) -> Document:
        """加载指定文档；省略路径时使用 sources.toml 的 default_document。"""
        key = self._validate_key(
            self._config.default_document if document_key is None else document_key
        )
        product = key.parts[0]
        source_config = self._config.sources.get(product)
        if source_config is None:
            raise DocumentLoadError(f"sources.toml 尚未声明数据源：{product}")

        raw_path = self._resolve_file("raw", key)
        processed_path = self._resolve_file("processed", key)
        try:
            with processed_path.open("rb") as file:
                payload = file.read(self.max_file_bytes + 1)
        except OSError as exc:
            raise DocumentLoadError(f"读取文档失败 {processed_path}：{exc}") from exc
        if len(payload) > self.max_file_bytes:
            raise DocumentLoadError(
                f"文档超过加载上限 {self.max_file_bytes} 字节：{key.as_posix()}"
            )
        try:
            # 兼容 Windows UTF-8 BOM 和 CRLF；不改写正文及其首尾空行。
            content = payload.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise DocumentLoadError(f"文档必须使用 UTF-8 编码：{processed_path}") from exc
        if not content.strip():
            raise DocumentLoadError(f"文档正文为空：{processed_path}")
        if content.splitlines()[0].strip() == "---":
            raise DocumentLoadError("清洗后的正文仍有 YAML 头部，请先完成清洗再加载")

        title = self._extract_title(content)
        raw_relative = raw_path.relative_to(self.corpus_root).as_posix()
        processed_relative = processed_path.relative_to(self.corpus_root).as_posix()
        document_config = self._config.documents.get(key.as_posix())
        if document_config is None:
            source = raw_relative
            source_type = "local_file"
            logger.warning("%s 未登记网页 URL，source 使用原始文件相对路径", key.as_posix())
        else:
            source = str(document_config.source_url)
            source_type = "web"

        # 文档身份由语料路径决定；正文变化由独立的内容摘要表示。
        document_id = hashlib.sha256(key.as_posix().encode("utf-8")).hexdigest()
        metadata = {
            "metadata_schema_version": 1,
            "document_id": document_id,
            "title": title,
            "product": product,
            "language": source_config.language,
            "source": source,
            "source_type": source_type,
            "raw_path": raw_relative,
            "processed_path": processed_relative,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            # 与项目现有检索工具使用的字段保持兼容。
            "_source": processed_relative,
            "_extension": processed_path.suffix.lower(),
            "_file_name": processed_path.name,
        }
        logger.info("文档加载完成：%s，正文 %d 字符", key.as_posix(), len(content))
        return Document(page_content=content, metadata=metadata)

    @staticmethod
    def _validate_key(value: str) -> PurePosixPath:
        # 只接受跨平台相对路径；拒绝盘符、反斜杠及路径穿越。
        if "\\" in value or ":" in value or "\x00" in value:
            raise DocumentLoadError("文档路径须使用正斜杠，例如 kubernetes/debug-pods.md")
        key = PurePosixPath(value)
        if key.is_absolute() or ".." in key.parts or len(key.parts) < 2:
            raise DocumentLoadError("文档路径须为语料目录内的相对路径：产品名/文档.md")
        if key.suffix.lower() != ".md":
            raise DocumentLoadError("本阶段仅支持已清洗的 Markdown（.md）文件")
        return key

    def _resolve_file(self, stage: str, key: PurePosixPath) -> Path:
        stage_root = (self.corpus_root / stage).resolve()
        path = stage_root.joinpath(*key.parts).resolve()
        if not stage_root.is_relative_to(self.corpus_root) or not path.is_relative_to(stage_root):
            raise DocumentLoadError(f"文档路径越出 {stage} 目录：{key.as_posix()}")
        if not path.is_file():
            hint = "请先准备原始文件" if stage == "raw" else "请先运行对应的清洗脚本"
            raise DocumentLoadError(f"找不到文档 {path}；{hint}")
        return path

    @staticmethod
    def _extract_title(content: str) -> str:
        """提取代码围栏、HTML 注释以外的首个 ATX 一级标题。"""
        visible = re.sub(r"<!--.*?-->", "", content, flags=re.S)
        fence_char = ""
        fence_length = 0
        for line in visible.splitlines():
            fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if fence_char:
                if (
                    fence
                    and fence.group(1)[0] == fence_char
                    and len(fence.group(1)) >= fence_length
                    and not fence.group(2).strip()
                ):
                    fence_char = ""
                continue
            if fence:
                fence_char = fence.group(1)[0]
                fence_length = len(fence.group(1))
                continue
            heading = re.match(r"^ {0,3}#[ \t]+(.+?)\s*$", line)
            if heading:
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(1)).strip()
                if title:
                    return title
        raise DocumentLoadError("清洗后的 Markdown 缺少有效的一级标题（# 标题）")
