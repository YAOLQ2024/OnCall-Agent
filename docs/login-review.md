# 登录功能审查与修补（2026-10-24）

本轮仅修复登录链路和相关接口/状态管理，未修改 UI 风格、HTML 或 CSS。保留现有登录表单、演示身份卡片和默认演示账号。

## 已确认并修复的问题

| 问题 | 修补 |
| --- | --- |
| 未登录仍默认 Alice，旧身份 Cookie、X-User-Id 可绕过密码 | 业务接口只接受有效 Bearer 或签名 Cookie；默认身份与演示 Header 不再授予权限 |
| /auth/switch 无密码直接签发任意用户 Token | 该旧接口返回 410，切换统一使用现有账号密码登录；快捷卡片仍走 login |
| 工具状态/刷新、文件上传和知识库接口未统一设登录守卫 | 在实际 FastAPI 应用注册这些路由时加入认证依赖；主页、静态资源和健康检查仍可访问 |
| 硬编码且公开的默认 JWT 密钥可被用于伪造 | 显式 ONCALL_JWT_SECRET 优先；空值在本机 SQLite 随机生成并持久保存，重启不换密钥；不记录或输出密钥 |
| JWT 不检查算法，缺 exp/exp=0 可不失效 | 校验 HS256/typ、签名、规范 base64url、必需的 sub/iat/exp/jti/iss/aud；到达 exp 即失效；无效 Authorization 不降级使用其他身份 |
| 同账号同一秒签发相同 Token；退出只清 Cookie，Bearer 可重放 | 每次签发独立随机 jti；退出将当前 jti 撤销到其过期时间，撤销状态保存在 SQLite；其他登录设备的 Token 不受影响 |
| 密码使用固定盐和 10,000 次迭代 | 新哈希使用随机盐和 PBKDF2-HMAC-SHA256 600,000 次迭代，参数随哈希保存；旧格式成功登录后升级；不重置现有自定义密码；密码计算移到线程池 |
| 401 后聊天记录和旧用户数据还留在内存/DOM | 清理用户、会话和密码输入，终止旧认证请求，返回现有登录层 |
| 较晚返回的旧账号请求/401 可覆盖新账号或把新账号登出 | 为认证请求加入版本检查，覆盖 fetch 返回、JSON 读取和 SSE reader；旧请求不应用到新身份；跨标签页 Token 变化同步清理 |
| 快捷卡片双击/登录和退出同时请求可能串号 | 统一认证操作忙碌标记；退出未被服务器确认时保留可重试登录状态 |
| 清空 localStorage 后，即使存在有效 HttpOnly Cookie 仍强制显示未登录 | 初始化向 /auth/me 校验 Cookie，恢复有效登录态 |
| 工具轮询绕过统一 authFetch | 统一鉴权请求；登录后立即加载工具状态，未登录不轮询 |
| 旧 UI 测试仍调用已移除的 switchUser | 按现有 login/logout 流程更新，并补充过期、并发和旧响应回归 |

密码参数参考 [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)；退出时撤销唯一 jti 的依据见 [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)。JWT 的每次请求验签与本地撤销表相结合，因此不能再称为完全不维护服务端状态。

## 验证结果

- 项目现有 Windows Python 3.12 虚拟环境：**157 passed，40.59 秒**。4 条现有 Pydantic Config 弃用提示，无失败。
- Node 前端逻辑测试：**15 passed**，使用实际 app.js 与受控 DOM/fetch，不依赖外部服务。
- Python 回归默认拦截真实模型 HTTP；完整应用路由/生命周期与真实 SQLite/LangGraph 的集成使用模型及外部基础设施替身，未用真实模型回答作为验收依据。
- 当前运行服务 127.0.0.1:9900 的真实 HTTP 验证通过：匿名访问、X-User-Id 绕过、旧 switch 均被拒绝；错密返回 401；正确密码登录/HttpOnly Cookie/身份读取正常；Bob 不能靠 Header 访问 Alice 的临时会话；退出后的 Bearer 重放返回 401，Bob 的独立登录仍有效；临时会话已删除。
- node --check 和 git diff --check 通过；未安装依赖、未提交或推送，未重启用户进程。
- 未进行真实浏览器截图或设备视觉验收。HTML 与 CSS 检查前后的 SHA-256 完全一致：

```text
static/index.html  6c323915e339fabeca190394b424abb4e337d2f85ac2573cf55669027e9a6191
static/styles.css  4deb40585bf436611f183e860783783dcbf4d8befbb397608864907c81cc28af
```

重跑：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -o addopts="" -q
node --test tests/session-ui.test.cjs
```

## 使用与边界

现在强制刷新页面并重新登录即可；此前由公开默认密钥签发的旧版 Token 不再接受。演示账号 alice / bob / admin 及快捷卡片保留，演示密码仍为 123456。登录功能不新增角色专属提示词，也不赋予 admin 额外工具权限。

.env.example 与本机 .env 同步增加 ONCALL_JWT_SECRET 空配置项，保留原有配置值。空值的本地随机密钥随会话数据库保存，备份数据库时应一并保护。生产部署仍需独立配置共享密钥、数据库、限流和角色权限；当前保留公开演示密码，不能据此宣称已完成生产认证系统。
