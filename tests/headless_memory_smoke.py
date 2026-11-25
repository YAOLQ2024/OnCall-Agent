"""Real-browser memory smoke. Use --m2 for incidents, --m3 for recall, or --m4 for extraction and review.

Run with the project's Windows Python. No real model/Milvus/MCP calls are made.
"""

import asyncio
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_TRACING_V2"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect
from test_session_api import SessionModel, integrated_app


class CDP:
    def __init__(self, websocket):
        self.websocket, self.counter = websocket, 0

    def command(self, method, **params):
        self.counter += 1
        self.websocket.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            result = json.loads(self.websocket.recv(timeout=15))
            if result.get("id") == self.counter:
                if "error" in result:
                    raise RuntimeError(result["error"]["message"])
                return result.get("result", {})

    def evaluate(self, expression):
        result = self.command("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "browser script failed"))
        return result.get("result", {}).get("value")

    def wait(self, expression):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise TimeoutError(expression)

    def screenshot(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.command("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
        path.write_bytes(base64.b64decode(data))


def run_m2(cdp, client):
    """Exercise M2 from the shipped DOM using an isolated test app and database."""
    def click(id):
        cdp.evaluate(f"document.getElementById({id!r}).click()")

    def value(id, content):
        cdp.evaluate(f"""(() => {{
            const input = document.getElementById({id!r});
            input.value = {json.dumps(content)};
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})()""")

    def wait_text(id, expected):
        cdp.wait(f"document.getElementById({id!r}).textContent.includes({json.dumps(expected)})")

    def search():
        value("memoryIncidentQuery", "ERR-503")
        cdp.evaluate("document.getElementById('memoryIncidentSearch').requestSubmit()")

    click("memoryBtn")
    click("memoryIncidentTab")
    wait_text("memoryIncidentStatus", "暂无符合条件")
    click("memoryIncidentNew")
    value("incidentTitle", "<b>支付连接池</b>")
    value("incidentSymptoms", "连接池告警 ERR-503")
    value("incidentServices", "pay-api")
    value("incidentErrors", "ERR-503")
    value("incidentEnvironment", "production")
    value("incidentOccurred", "2025-09-01T10:00")
    value("incidentSession", "m2-browser-source")
    cdp.evaluate("document.getElementById('memoryIncidentSource').open=true")
    click("memoryIncidentMessages")
    wait_text("memoryIncidentMessageList", "browser recovery evidence")
    message_id = client.get("/api/sessions/m2-browser-source/message-references").json()["data"]["messages"][0]["id"]
    value("incidentMessageIds", str(message_id))
    click("memoryIncidentSave")
    wait_text("memoryIncidentMeta", "版本 1")
    click("memoryIncidentBack")
    wait_text("memoryIncidentList", "<b>支付连接池</b>")
    assert not cdp.evaluate("document.querySelector('#memoryIncidentList b') !== null")
    search()
    wait_text("memoryIncidentStatus", "暂无符合条件")
    click("memoryIncidentAll")
    wait_text("memoryIncidentList", "待确认")
    cdp.evaluate("document.querySelector('#memoryIncidentList button').click()")
    wait_text("memoryIncidentMeta", "版本 1")
    cdp.evaluate("document.getElementById('memoryIncidentConfirmation').open=true")
    click("memoryIncidentResolved")
    wait_text("memoryIncidentStatus", "恢复证据")
    value("incidentRecovery", "错误率恢复到基线")
    value("incidentRecoveryIds", str(message_id))
    click("memoryIncidentSave")
    wait_text("memoryIncidentMeta", "版本 2")
    cdp.evaluate("document.getElementById('memoryIncidentConfirmation').open=true")
    click("memoryIncidentResolved")
    wait_text("memoryIncidentMeta", "已恢复")
    click("memoryIncidentBack")
    wait_text("memoryIncidentList", "已确认")
    search()
    wait_text("memoryIncidentStatus", "已搜索")
    wait_text("memoryIncidentList", "支付连接池")

    cdp.command("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
    cdp.evaluate("document.querySelector('#memoryIncidentList button').click()")
    wait_text("memoryIncidentMeta", "版本 3")
    cdp.evaluate("document.getElementById('memoryIncidentSource').open=true")
    assert cdp.evaluate("""(() => {
        const d = document.getElementById('memoryDialog'), r = d.getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth && d.scrollWidth <= d.clientWidth;
    })()""")

    incidents = "/api/memory/incidents"
    mid = client.get(incidents).json()["data"]["items"][0]["memory_id"]
    path = f"{incidents}/{mid}"
    assert client.patch(path, json={"expected_revision": 3, "changes": {"symptoms": "updated externally"}}).status_code == 200
    value("incidentSymptoms", "unsaved browser edit")
    click("memoryIncidentSave")
    wait_text("memoryIncidentStatus", "输入仍保留")
    assert cdp.evaluate("document.getElementById('incidentSymptoms').value") == "unsaved browser edit"
    assert cdp.evaluate("document.getElementById('memoryIncidentSave').disabled")
    click("memoryIncidentReload")
    wait_text("memoryIncidentMeta", "版本 4")
    click("memoryIncidentBack")
    wait_text("memoryIncidentList", "待确认")
    search()
    wait_text("memoryIncidentStatus", "暂无符合条件")
    click("memoryIncidentAll")
    wait_text("memoryIncidentList", "待确认")
    cdp.evaluate("document.querySelector('#memoryIncidentList button').click()")
    wait_text("memoryIncidentMeta", "版本 4")
    cdp.evaluate("document.getElementById('memoryIncidentWithdrawal').open=true")
    click("memoryIncidentWithdraw")
    wait_text("memoryIncidentStatus", "正文和索引已清除")
    assert cdp.evaluate("document.getElementById('incidentTitle').value") == ""
    # Close and reopen before changing users, then verify the real logout/login route.
    click("closeMemoryBtn")
    click("memoryBtn")
    click("memoryIncidentTab")
    wait_text("memoryIncidentList", "已撤回事故")
    click("closeMemoryBtn")
    click("logoutBtn")
    cdp.wait("!document.getElementById('loginOverlay').classList.contains('hidden')")
    value("loginUsername", "bob")
    value("loginPassword", "123456")
    cdp.evaluate("document.getElementById('loginForm').requestSubmit()")
    cdp.wait("document.getElementById('userName').textContent.trim()==='Bob' && document.getElementById('loginOverlay').classList.contains('hidden')")
    click("memoryBtn")
    click("memoryIncidentTab")
    wait_text("memoryIncidentStatus", "暂无符合条件")
    assert cdp.evaluate("document.getElementById('memoryIncidentList').children.length") == 0
    print("BROWSER_M2_OK: draft, evidence, confirm, lexical search, 409/reload, withdrawal, narrow layout, Alice/Bob isolation")



class MemoryBrowserModel(SessionModel):
    def _answer(self, messages):
        prefix = "以下 JSON 是历史参考数据，与本轮用户请求分开处理。\n"
        references = [message.content[len(prefix):] for message in messages
                      if isinstance(message.content, str) and message.content.startswith(prefix)]
        assert len(references) <= 1, "Long-term references accumulated in the model view"
        data = json.loads(references[0]) if references else {}
        profile = data.get("profile", {})
        language = profile.get("preferred_language", "none")
        detail = profile.get("answer_detail", "none")
        return f"M3 model received {language}/{detail}; cases={len(data.get('historical_incidents', []))}"


def run_m3(cdp, client):
    def click(id):
        cdp.evaluate(f"document.getElementById({id!r}).click()")

    def value(id, content):
        cdp.evaluate(f"document.getElementById({id!r}).value = {json.dumps(content)}")

    def wait_text(id, expected):
        cdp.wait(f"document.getElementById({id!r}).textContent.includes({json.dumps(expected)})")

    def profile(language, detail):
        click("memoryBtn")
        cdp.wait("!document.getElementById('memoryProfileFields').disabled")
        value("memoryLanguage", language)
        value("memoryDetail", detail)
        value("memoryEnvironment", "production")
        cdp.evaluate("document.getElementById('memoryUseEnabled').checked = true")
        click("memorySaveBtn")
        wait_text("memoryProfileStatusText", "画像已保存")
        click("closeMemoryBtn")

    def login(name, display_name):
        click("logoutBtn")
        cdp.wait("!document.getElementById('loginOverlay').classList.contains('hidden')")
        value("loginUsername", name)
        value("loginPassword", "123456")
        cdp.evaluate("document.getElementById('loginForm').requestSubmit()")
        cdp.wait(f"document.getElementById('loginOverlay').classList.contains('hidden') && "
                 f"document.getElementById('userName').textContent.trim() === {display_name!r}")

    def send(language, detail, *, case=None, new_chat=True):
        if new_chat:
            click("newChatBtn")
        value("messageInput", "连接池告警 ERR-503")
        click("sendButton")
        cdp.wait("!document.getElementById('sendButton').disabled")
        expect = f"M3 model received {language}/{detail}; cases={int(case is not None)}"
        assistant = "[...document.querySelectorAll('#chatMessages .message.assistant')].at(-1)"
        cdp.wait(f"({assistant})?.querySelector('.message-content')?.textContent.includes({json.dumps(expect)})")
        badge = cdp.evaluate(f"({assistant})?.querySelector('.memory-recall-badge')?.textContent")
        assert badge, "Missing memory badge"
        if case:
            assert "已参考事故经验" in badge and case in badge, badge
            assert ("BOB-PRIVATE" if case == "ALICE-PRIVATE" else "ALICE-PRIVATE") not in badge
        else:
            assert "已参考个人画像" in badge and "PRIVATE" not in badge, badge

    profile("zh-CN", "concise")
    send("zh-CN", "concise", case="ALICE-PRIVATE")
    click("modeSelectorBtn")
    cdp.evaluate("document.querySelector('[data-mode=quick]').click()")
    send("zh-CN", "concise", case="ALICE-PRIVATE")
    sessions = client.get("/api/sessions").json()["data"]["sessions"]
    assert len(sessions) == 2 and len({session["session_id"] for session in sessions}) == 2
    login("bob", "Bob")
    profile("en", "detailed")
    send("en", "detailed", case="BOB-PRIVATE")
    assert "ALICE-PRIVATE" not in cdp.evaluate("document.getElementById('chatMessages').textContent")

    login("alice", "Alice")
    click("modeSelectorBtn")
    cdp.evaluate("document.querySelector('[data-mode=stream]').click()")
    send("zh-CN", "concise", case="ALICE-PRIVATE")
    click("memoryBtn")
    click("memoryIncidentTab")
    wait_text("memoryIncidentList", "ALICE-PRIVATE")
    assert "BOB-PRIVATE" not in cdp.evaluate("document.getElementById('memoryIncidentList').textContent")
    cdp.evaluate("document.querySelector('#memoryIncidentList button').click()")
    wait_text("memoryIncidentMeta", "已确认")
    cdp.evaluate("document.getElementById('memoryIncidentWithdrawal').open = true")
    click("memoryIncidentWithdraw")
    wait_text("memoryIncidentStatus", "正文和索引已清除")
    click("memoryIncidentBack")
    wait_text("memoryIncidentList", "已撤回事故")
    value("memoryIncidentQuery", "ERR-503")
    cdp.evaluate("document.getElementById('memoryIncidentSearch').requestSubmit()")
    wait_text("memoryIncidentStatus", "暂无符合条件")
    click("closeMemoryBtn")
    send("zh-CN", "concise", new_chat=False)

    login("bob", "Bob")
    send("en", "detailed", case="BOB-PRIVATE")
    print("BROWSER_M3_OK: profile reached the model in separate sessions, Alice/Bob isolated, withdrawal removed lexical hit and next-turn recall")



def run_m4(cdp, client, db_path, late_started, late_release, late_returned):
    def click(id):
        cdp.evaluate(f"document.getElementById({id!r}).click()")

    def wait_text(id, expected):
        cdp.wait(f"document.getElementById({id!r}).textContent.includes({json.dumps(expected)})")

    with httpx.Client(base_url=client.base_url, timeout=10, trust_env=False) as bob:
        assert bob.post("/api/auth/login", json={"username": "bob", "password": "123456"}).status_code == 200
        assert bob.patch("/api/memory/profile", json={
            "expected_revision": 0, "changes": {"preferred_language": "en", "answer_detail": "detailed",
                                                "response_preferences": ["使用一个短例子"]},
        }).status_code == 200
        created = bob.post("/api/memory/incidents", json={
            "title": "BOB-PRIVATE-INCIDENT", "symptoms": "ERR-503", "services": ["pay-api"],
            "environment": "production", "occurred_at": "2025-01-01T00:00:00Z",
        })
        assert created.status_code == 201, created.text
        bob_incident_id = created.json()["data"]["memory_id"]
        assert bob.post(f"/api/memory/incidents/{bob_incident_id}/confirm", json={
            "expected_revision": 1, "outcome": "unresolved",
        }).status_code == 200
        bob_job_id = bob.get("/api/memory/jobs").json()["data"]["jobs"][0]["job_id"]
    assert client.get(f"/api/memory/jobs/{bob_job_id}").status_code == 404

    click("memoryBtn")
    cdp.wait("!document.getElementById('memoryProfileFields').disabled")
    cdp.evaluate("document.getElementById('memoryAutoExtract').checked = true; document.getElementById('memoryUseEnabled').checked = true")
    click("memorySaveBtn")
    wait_text("memoryProfileStatusText", "画像已保存")
    click("closeMemoryBtn")
    cdp.evaluate("document.getElementById('messageInput').value = 'chat 以后默认先给结论，英文术语旁附中文翻译'")
    click("sendButton")
    cdp.wait("!document.getElementById('sendButton').disabled")
    click("memoryBtn")
    cdp.wait("!document.getElementById('memoryProfileFields').disabled")
    click("memoryJobsTab")
    wait_text("memoryJobList", "画像提议")
    cdp.wait("document.getElementById('memoryJobSession').options.length > 1")
    cdp.evaluate("document.querySelector('#memoryJobList button').click()")
    wait_text("memoryJobHeading", "待审核")
    assert cdp.evaluate("document.getElementById('memoryJobProposal').textContent.includes('自然语言回答偏好')")
    assert cdp.evaluate("document.getElementById('memoryJobProposal').textContent.includes('英文术语旁附中文翻译')")
    assert client.get("/api/memory/profile").json()["data"]["profile"] == {}
    alice_job_id = client.get("/api/memory/jobs").json()["data"]["jobs"][0]["job_id"]
    with httpx.Client(base_url=client.base_url, timeout=10, trust_env=False) as bob:
        assert bob.post("/api/auth/login", json={"username": "bob", "password": "123456"}).status_code == 200
        assert bob.get(f"/api/memory/jobs/{alice_job_id}").status_code == 404
        assert bob.post(f"/api/memory/jobs/{alice_job_id}/review", json={"action": "apply"}).status_code == 404
    click("memoryJobApply")
    wait_text("memoryJobStatus", "已更新个人画像")
    assert client.get("/api/memory/profile").json()["data"]["profile"] == {
        "preferred_output_order": "summary_first", "response_preferences": ["英文术语旁附中文翻译"],
    }
    click("memoryProfileTab")
    assert cdp.evaluate("document.getElementById('memoryResponsePreferences').value") == "英文术语旁附中文翻译"
    cdp.evaluate("document.getElementById('memoryResponsePreferences').value = '英文术语旁附中文翻译\\n用短句总结'")
    click("memorySaveBtn")
    wait_text("memoryProfileStatusText", "画像已保存")
    assert client.get("/api/memory/profile").json()["data"]["profile"]["response_preferences"] == ["英文术语旁附中文翻译", "用短句总结"]
    click("closeMemoryBtn")
    click("newChatBtn")
    cdp.evaluate("document.getElementById('messageInput').value = 'chat DNS 是什么'")
    click("sendButton")
    cdp.wait("!document.getElementById('sendButton').disabled && document.querySelector('.memory-recall-badge')?.textContent.includes('已参考个人画像')")
    click("memoryBtn")

    incident_response = client.post("/api/chat", json={
        "Id": "m4-browser-incident", "Question": "aiops 支付服务报错 ERR-503",
    })
    assert incident_response.status_code == 200, incident_response.text
    incident_job_id = incident_response.json()["data"]["memory_job_id"]
    click("memoryProfileTab")
    click("memoryJobsTab")
    cdp.wait("document.querySelector('#memoryJobSession option[value=m4-browser-incident]') !== null")

    cdp.evaluate("""const select = document.getElementById('memoryJobSession');
        select.value = 'm4-browser-incident';
        select.dispatchEvent(new Event('change'));""")
    click("memoryJobSubmit")
    wait_text("memoryJobHeading", "事故提议 · 待审核")
    assert client.get(f"/api/memory/jobs/{incident_job_id}").json()["data"]["requires_confirmation"]
    assert client.get("/api/memory/incidents").json()["data"]["items"] == []
    click("memoryJobApply")
    wait_text("memoryJobStatus", "已保存事故草稿")
    assert client.get("/api/memory/incidents").json()["data"]["items"][0]["status"] == "draft"

    late_response = client.post("/api/chat", json={
        "Id": "m4-browser-late", "Question": "chat 今后默认步骤优先，首次出现的术语再附翻译",
    })
    assert late_response.status_code == 200, late_response.text
    late_job_id = late_response.json()["data"]["memory_job_id"]
    assert late_started.wait(timeout=10), "Test extractor did not start the delayed job"
    assert client.get(f"/api/memory/jobs/{late_job_id}").json()["data"]["status"] == "running"
    cdp.evaluate("""document.getElementById('memoryForgetSection').open = true;
        document.getElementById('memoryForgetCheck').checked = true;
        document.getElementById('memoryForgetForm').requestSubmit();""")
    wait_text("memoryJobStatus", "已清空长期记忆")
    late_release.set()
    assert late_returned.wait(timeout=10), "The delayed extractor did not return a valid result"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(db_path)) as conn:
            lease_token, result = conn.execute(
                "SELECT lease_token, result_json FROM ltm_jobs WHERE job_id=?", (late_job_id,),
            ).fetchone()
        if lease_token is None:
            assert result is None
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("Delayed model response did not finish after forget")
    late_job = client.get(f"/api/memory/jobs/{late_job_id}").json()["data"]
    assert late_job["status"] == "cancelled" and "proposal" not in late_job
    assert client.post(f"/api/memory/jobs/{late_job_id}/review", json={"action": "apply"}).status_code == 409
    profile = client.get("/api/memory/profile").json()["data"]
    assert profile["profile"] == {} and profile["generation"] == 1
    assert client.get("/api/memory/incidents").json()["data"]["items"][0]["status"] == "withdrawn"
    assert client.get("/api/sessions").json()["data"]["sessions"]
    click("closeMemoryBtn")
    click("logoutBtn")
    cdp.wait("!document.getElementById('loginOverlay').classList.contains('hidden')")
    cdp.evaluate("""document.getElementById('loginUsername').value = 'bob';
        document.getElementById('loginPassword').value = '123456';
        document.getElementById('loginForm').requestSubmit();""")
    cdp.wait("document.getElementById('userName').textContent.trim() === 'Bob'")
    click("memoryBtn")
    click("memoryJobsTab")
    wait_text("memoryJobUsage", "本账号今日用量")
    assert cdp.evaluate("[...document.querySelectorAll('#memoryJobList button')].map(b => b.dataset.jobId)") == [bob_job_id]
    click("memoryProfileTab")
    cdp.wait("!document.getElementById('memoryProfileFields').disabled")
    assert cdp.evaluate("document.getElementById('memoryLanguage').value") == "en"
    assert cdp.evaluate("document.getElementById('memoryDetail').value") == "detailed"
    assert cdp.evaluate("document.getElementById('memoryResponsePreferences').value") == "使用一个短例子"
    click("memoryIncidentTab")
    wait_text("memoryIncidentList", "BOB-PRIVATE-INCIDENT")
    assert "连接池故障" not in cdp.evaluate("document.getElementById('memoryIncidentList').textContent")
    print("BROWSER_M4_OK: editable natural-language preference, review and new chat, forget rejects a late result, Alice/Bob isolated")


def main():
    m2 = "--m2" in sys.argv
    m3 = "--m3" in sys.argv
    m4 = "--m4" in sys.argv
    candidates = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                  Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")]
    binary = next((path for path in candidates if path.is_file()), None)
    if binary is None:
        raise RuntimeError("An existing Chrome or Edge installation is required")
    output = Path(__file__).resolve().parents[1] / "docs" / "validation"
    with tempfile.TemporaryDirectory(prefix="oncall-memory-browser-") as temporary:
        temporary = Path(temporary)
        patches = pytest.MonkeyPatch()
        server = process = None
        browser_url = None
        thread = None
        late_started, late_release, late_returned = threading.Event(), threading.Event(), threading.Event()
        try:
            app, db, saver, rag, aiops = integrated_app.__wrapped__(temporary, patches)
            if m2:
                db.record_message("m2-browser-source", "user", "browser recovery evidence", "sre_alice")
            if m3:
                from app.api import memory
                from app.config import config
                from app.core.llm_factory import llm_factory
                from test_memory_incidents import ALICE, BOB, confirm, create

                patches.setattr(config, "ltm_enabled", True)
                patches.setattr(config, "ltm_vector_enabled", False)
                patches.setattr(rag, "model", MemoryBrowserModel())
                patches.setattr(llm_factory, "create_chat_model", lambda **kwargs: MemoryBrowserModel())
                incidents = memory.memory_service.incidents
                for uid, title in ((ALICE, "ALICE-PRIVATE"), (BOB, "BOB-PRIVATE")):
                    confirm(incidents, create(incidents, uid, title=title), uid)
            if m4:
                from app.config import config
                from app.services.memory_extraction_service import MemoryExtractionService, ProfileProposal

                for key, value in {
                    "ltm_enabled": True, "ltm_auto_extract_enabled": True, "ltm_paid_calls_enabled": True,
                    "ltm_vector_enabled": False, "ltm_daily_input_token_budget": 100_000,
                    "ltm_daily_output_token_budget": 10_000,
                }.items():
                    patches.setattr(config, key, value)

                async def extract(self, schema, messages):
                    source = next(row for row in json.JSONDecoder().raw_decode(messages[-1].content)[0]
                                  if row["role"] == "user")
                    late = False
                    if schema is ProfileProposal:
                        late = "今后默认步骤优先" in source["text"]
                        if late:
                            late_started.set()
                            if not await asyncio.to_thread(late_release.wait, 20):
                                raise TimeoutError("Browser did not release delayed extraction")
                        data = {"changes": {"preferred_output_order": "steps_first" if late else "summary_first"},
                                "evidence": [{"field": "preferred_output_order", "message_id": source["id"],
                                              "quote": "今后默认步骤优先" if late else "以后默认先给结论"}]}
                        preference = "首次出现的术语再附翻译" if late else "英文术语旁附中文翻译"
                        data["preference_changes"] = [{"text": preference,
                                                       "evidence": {"message_id": source["id"], "quote": preference}}]
                    else:
                        data = {"draft": {"title": "连接池故障", "symptoms": "ERR-503"},
                                "evidence": [{"message_id": source["id"], "quote": "支付服务报错 ERR-503"}]}
                    parsed = schema.model_validate(data)
                    if late:
                        late_returned.set()
                    return {"raw": None, "parsed": parsed, "parsing_error": None}

                patches.setattr(MemoryExtractionService, "extract", extract)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
            thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Temporary app did not start")
                time.sleep(0.05)
            with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
                result = client.post("/api/auth/login", json={"username": "alice", "password": "123456"})
                assert result.status_code == 200
                alice_token = result.json()["data"]["access_token"]
                profile = temporary / "browser"
                process = subprocess.Popen([str(binary), "--headless=new", "--remote-debugging-port=0",
                    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                    "--disable-background-networking", "--disable-extensions", "about:blank"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                active = profile / "DevToolsActivePort"
                deadline = time.monotonic() + 15
                while not active.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Headless browser did not start")
                    time.sleep(0.05)
                port, browser_path = active.read_text().splitlines()[:2]
                browser_url = f"ws://127.0.0.1:{port}{browser_path}"
                target = next(t for t in httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json() if t["type"] == "page")
                with connect(target["webSocketDebuggerUrl"], max_size=None) as websocket:
                    cdp = CDP(websocket)
                    cdp.command("Page.enable")
                    cdp.command("Runtime.enable")
                    cdp.command("Network.enable")
                    cdp.command("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
                    cdp.command("Network.setCookie", name="oncall_token", value=alice_token, url=base, httpOnly=True, sameSite="Strict")
                    cdp.command("Page.navigate", url=base)
                    cdp.wait("document.getElementById('loginOverlay')?.classList.contains('hidden')")
                    if m2:
                        cdp.wait("document.querySelector('#chatHistoryList')?.textContent.includes('browser recovery evidence')")
                        run_m2(cdp, client)
                        return
                    if m3:
                        cdp.wait("document.getElementById('userName')?.textContent.trim()==='Alice'")
                        run_m3(cdp, client)
                        return
                    if m4:
                        cdp.wait("document.getElementById('userName')?.textContent.trim()==='Alice'")
                        run_m4(cdp, client, db.db_path, late_started, late_release, late_returned)
                        return
                    cdp.evaluate("document.getElementById('memoryBtn').click()")
                    cdp.wait("document.getElementById('memoryDialog').open && !document.getElementById('memoryProfileFields').disabled")
                    cdp.evaluate("document.getElementById('memoryLanguage').value='zh-CN'; document.getElementById('memoryNamespace').value='prod-pay'; document.getElementById('memoryDetail').value='concise'; document.getElementById('memoryUseEnabled').checked=true; document.getElementById('memoryProfileForm').requestSubmit()")
                    cdp.wait("document.getElementById('memoryProfileOwner').textContent.includes('版本 1') && !document.getElementById('memorySaveBtn').disabled")
                    cdp.screenshot(output / "memory-m1-desktop.png")
                    saved = client.get("/api/memory/profile").json()["data"]
                    assert saved["profile"]["default_namespace"] == "prod-pay"
                    assert saved["use_enabled"] is True
                    assert client.patch("/api/memory/profile", json={"expected_revision": 1, "changes": {"default_namespace": "prod-other"}}).status_code == 200
                    cdp.evaluate("document.getElementById('memoryNamespace').value='staging'; document.getElementById('memoryProfileForm').requestSubmit()")
                    cdp.wait("document.getElementById('memoryProfileStatus').dataset.state==='warning' && document.getElementById('memorySaveBtn').disabled")
                    assert cdp.evaluate("document.getElementById('memoryNamespace').value") == "staging"
                    cdp.evaluate("document.getElementById('memoryReloadBtn').click()")
                    cdp.wait("document.getElementById('memoryNamespace').value==='prod-other' && !document.getElementById('memorySaveBtn').disabled")
                    cdp.command("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
                    assert cdp.evaluate("(()=>{const d=document.getElementById('memoryDialog'); const r=d.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && d.scrollWidth<=d.clientWidth})()")
                    cdp.screenshot(output / "memory-m1-narrow.png")
                    bob = client.post("/api/auth/login", json={"username": "bob", "password": "123456"}).json()["data"]["access_token"]
                    cdp.command("Network.setCookie", name="oncall_token", value=bob, url=base, httpOnly=True, sameSite="Strict")
                    cdp.command("Page.reload", ignoreCache=True)
                    cdp.wait("document.getElementById('userName')?.textContent.trim()==='Bob' && document.getElementById('loginOverlay').classList.contains('hidden')")
                    cdp.evaluate("document.getElementById('memoryBtn').click()")
                    cdp.wait("document.getElementById('memoryDialog').open && !document.getElementById('memoryProfileFields').disabled")
                    assert cdp.evaluate("document.getElementById('memoryNamespace').value") == ""
                    assert cdp.evaluate("document.getElementById('memoryProfileOwner').textContent.includes('版本 0')")
                    print("BROWSER_M1_OK: real DOM save/reload, 409 preserves edits, narrow layout, Alice/Bob isolation; only temporary app and DB used")
        finally:
            late_release.set()
            if browser_url:
                try:
                    with connect(browser_url, max_size=None) as websocket:
                        CDP(websocket).command("Browser.close")
                except Exception:
                    pass
            if process:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()  # Only this script's private browser process.
                    process.wait(timeout=5)
            if server:
                server.should_exit = True
            if thread:
                thread.join(timeout=10)
            patches.undo()


if __name__ == "__main__":
    main()
