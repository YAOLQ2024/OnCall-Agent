"""Versioned SQLite lexical index for private incident records."""

import json
import re
import sqlite3
import unicodedata

NORMALIZER_VERSION = 1
# Keep punctuation internal to technical identifiers and use overlapping Han bigrams.
_HAN = r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]"
_TECHNICAL = r"[a-z0-9]+(?:[._:/-]+[a-z0-9]+)*"
_WORDS = re.compile(f"(?P<han>{_HAN}+)|(?P<technical>{_TECHNICAL})")


def lexical_tokens(text: str):
    text = unicodedata.normalize("NFKC", text).casefold()
    for match in _WORDS.finditer(text):
        word = match.group()
        if match.lastgroup == "han" and len(word) > 1:
            yield from (word[i:i + 2] for i in range(len(word) - 1))
        else:
            yield word


def exact_identifiers(text: str):
    return _TECHNICAL_PATTERN.findall(unicodedata.normalize("NFKC", text).casefold())


_TECHNICAL_PATTERN = re.compile(_TECHNICAL)


def _encoded(token: str) -> str:
    # A full identifier becomes one alphanumeric token, even if it contains punctuation.
    return "t" + token.encode().hex()


def match_expression(query: str) -> str:
    terms = list(lexical_tokens(query))
    unique = list(dict.fromkeys(terms))
    if len(unique) > 128:
        # Keep late error codes even when a pasted log has many distinct words up front.
        tail = list(dict.fromkeys(reversed(terms)))[:64]
        unique = list(dict.fromkeys([*unique[:64], *reversed(tail)]))
    return " OR ".join(f'"{_encoded(token)}"' for token in unique)


def create_fts(conn: sqlite3.Connection):
    try:
        conn.execute("""CREATE VIRTUAL TABLE ltm_incidents_fts USING fts5(
            memory_id UNINDEXED, user_id UNINDEXED, revision UNINDEXED,
            normalizer_version UNINDEXED, normalized_tokens
        )""")
    except sqlite3.OperationalError as error:
        if str(error) != "no such module: fts5":
            raise
    else:
        if sqlite3.sqlite_version_info >= (3, 42, 0):
            conn.execute("INSERT INTO ltm_incidents_fts(ltm_incidents_fts, rank) VALUES ('secure-delete', 1)")


def fts_available(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ltm_incidents_fts'"
    ).fetchone() is not None


def remove_lexical(conn: sqlite3.Connection, uid: str, memory_id: str):
    if fts_available(conn):
        conn.execute("DELETE FROM ltm_incidents_fts WHERE user_id=? AND memory_id=?", (uid, memory_id))


def index_lexical(conn: sqlite3.Connection, row: sqlite3.Row):
    if not fts_available(conn):
        return
    fields = json.loads(row["incident_json"])
    parts = [fields.get(key, "") for key in (
        "title", "symptoms", "root_cause", "lessons", "cluster", "environment", "namespace",
    )]
    parts.extend(fields.get("services", []))
    parts.extend(fields.get("error_codes", []))
    tokens = (_encoded(token) for token in lexical_tokens(" ".join(parts)))
    conn.execute("INSERT INTO ltm_incidents_fts VALUES (?, ?, ?, ?, ?)", (
        row["memory_id"], row["user_id"], row["revision"], NORMALIZER_VERSION, " ".join(tokens),
    ))
