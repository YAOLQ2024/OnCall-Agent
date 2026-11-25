// OnCall Aegnt 前端应用
const EXECUTION_EVENT_TYPES = new Set(['route', 'aiops_progress', 'tool_call', 'search_results', 'compaction', 'debug']);

class OnCallAegntApp {
    constructor() {
        this.apiBaseUrl = '/api';
        this.isKnowledgeBusy = false;
        this.currentMode = 'stream'; // 默认开启流式推演模式，实时展示路由与思考轨迹
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.currentChatHistory = []; // 当前对话的消息历史
        this.chatHistories = []; // 所有历史对话
        this.isCurrentChatFromHistory = false; // 标记当前对话是否是从历史记录加载的
        this.accessToken = localStorage.getItem('oncall_access_token') || '';
        this.currentUser = null;
        this.memoryProfile = null;
        this.memoryProfileRequest = 0;
        this.memoryProfileBusy = false;
        this.memoryProfileConflict = false;
        this.authVersion = 0;
        this.authBusy = false;
        this.pendingAuthRequests = new Set();
        window.addEventListener('storage', (event) => {
            if (event.key !== 'oncall_access_token') return;
            this.clearAuthState();
            this.accessToken = event.newValue || '';
            if (this.accessToken) this.initAuth();
            else this.showLoginOverlay();
        });

        this.initializeElements();
        this.bindEvents();
        this.updateUI();
        this.initMarkdown();
        this.checkAndSetCentered();
        this.initAuth();
        this.loadToolStatus();
        this.toolStatusTimer = setInterval(() => {
            if (!document.hidden) this.loadToolStatus();
        }, 10000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) this.loadToolStatus();
        });
    }

    // 初始化Markdown配置
    initMarkdown() {
        // 等待 marked 库加载完成
        const checkMarked = () => {
            if (typeof marked !== 'undefined') {
                try {
                    // 配置marked选项
                    marked.setOptions({
                        breaks: true,  // 支持GFM换行
                        gfm: true,     // 启用GitHub风格的Markdown
                        headerIds: false,
                        mangle: false
                    });

                    // 配置代码高亮
                    if (typeof hljs !== 'undefined') {
                        marked.setOptions({
                            highlight: function(code, lang) {
                                if (lang && hljs.getLanguage(lang)) {
                                    try {
                                        return hljs.highlight(code, { language: lang }).value;
                                    } catch (err) {
                                        console.error('代码高亮失败:', err);
                                    }
                                }
                                return code;
                            }
                        });
                    }
                    console.log('Markdown 渲染库初始化成功');
                } catch (e) {
                    console.error('Markdown 配置失败:', e);
                }
            } else {
                // 如果 marked 还没加载，等待一段时间后重试
                setTimeout(checkMarked, 100);
            }
        };
        checkMarked();
    }

    // 安全地渲染 Markdown
    renderMarkdown(content) {
        if (!content) return '';

        // 检查 marked 是否可用
        if (typeof marked === 'undefined') {
            console.warn('marked 库未加载，使用纯文本显示');
            return this.escapeHtml(content);
        }

        try {
            const html = marked.parse(content);
            return html;
        } catch (e) {
            console.error('Markdown 渲染失败:', e);
            return this.escapeHtml(content);
        }
    }

    // 高亮代码块
    highlightCodeBlocks(container) {
        if (typeof hljs !== 'undefined' && container) {
            try {
                container.querySelectorAll('pre code').forEach((block) => {
                    if (!block.classList.contains('hljs')) {
                        hljs.highlightElement(block);
                    }
                });
            } catch (e) {
                console.error('代码高亮失败:', e);
            }
        }
    }

    // 初始化DOM元素
    initializeElements() {
        // 侧边栏元素
        this.sidebar = document.querySelector('.sidebar');
        this.newChatBtn = document.getElementById('newChatBtn');

        // 输入区域元素
        this.messageInput = document.getElementById('messageInput');
        this.sendButton = document.getElementById('sendButton');
        this.toolsBtn = document.getElementById('toolsBtn');
        this.toolsMenu = document.getElementById('toolsMenu');
        this.uploadFileItem = document.getElementById('uploadFileItem');
        this.modeSelectorBtn = document.getElementById('modeSelectorBtn');
        this.modeDropdown = document.getElementById('modeDropdown');
        this.currentModeText = document.getElementById('currentModeText');
        this.fileInput = document.getElementById('fileInput');
        this.knowledgeBtn = document.getElementById('knowledgeBtn');
        this.knowledgeDialog = document.getElementById('knowledgeDialog');
        this.closeKnowledgeBtn = document.getElementById('closeKnowledgeBtn');
        this.knowledgeUploadBtn = document.getElementById('knowledgeUploadBtn');
        this.reindexForm = document.getElementById('reindexForm');
        this.reindexBtn = document.getElementById('reindexBtn');
        this.indexDirectoryInput = document.getElementById('indexDirectoryInput');
        this.knowledgeResult = document.getElementById('knowledgeResult');
        this.knowledgeResultTitle = document.getElementById('knowledgeResultTitle');
        this.knowledgeResultDetails = document.getElementById('knowledgeResultDetails');
        this.toolStatusPanel = document.getElementById('toolStatusPanel');
        this.toolStatusSummary = document.getElementById('toolStatusSummary');
        this.toolStatusList = document.getElementById('toolStatusList');
        this.refreshToolsBtn = document.getElementById('refreshToolsBtn');
        this.toolStatusNotice = document.getElementById('toolStatusNotice');

        // 聊天区域元素
        this.chatMessages = document.getElementById('chatMessages');
        this.loadingOverlay = document.getElementById('loadingOverlay');
        this.chatContainer = document.querySelector('.chat-container');
        this.welcomeGreeting = document.getElementById('welcomeGreeting');
        this.chatHistoryList = document.getElementById('chatHistoryList');

        // 登录与用户元素
        this.loginOverlay = document.getElementById('loginOverlay');
        this.loginForm = document.getElementById('loginForm');
        this.loginUsername = document.getElementById('loginUsername');
        this.loginPassword = document.getElementById('loginPassword');
        this.loginError = document.getElementById('loginError');
        this.loginSubmitBtn = document.getElementById('loginSubmitBtn');
        this.logoutBtn = document.getElementById('logoutBtn');
        this.quickRoleCards = document.querySelectorAll('.quick-role-card');
        this.memoryBtn = document.getElementById('memoryBtn');
        this.memoryDialog = document.getElementById('memoryDialog');
        if (typeof IncidentMemoryView !== 'undefined') this.incidentMemory = new IncidentMemoryView(this);
        this.closeMemoryBtn = document.getElementById('closeMemoryBtn');
        this.memoryProfileForm = document.getElementById('memoryProfileForm');
        this.memoryProfileFields = document.getElementById('memoryProfileFields');
        this.memoryProfileOwner = document.getElementById('memoryProfileOwner');
        this.memorySaveBtn = document.getElementById('memorySaveBtn');
        this.memoryClearBtn = document.getElementById('memoryClearBtn');
        this.memoryReloadBtn = document.getElementById('memoryReloadBtn');
        this.memoryProfileStatus = document.getElementById('memoryProfileStatus');
        this.memoryProfileStatusText = document.getElementById('memoryProfileStatusText');
        this.memoryUseEnabled = document.getElementById('memoryUseEnabled');
        this.memoryAutoExtract = document.getElementById('memoryAutoExtract');
        this.memoryAutoHelp = document.getElementById('memoryAutoHelp');
        if (typeof MemoryJobsView !== 'undefined') this.memoryJobs = new MemoryJobsView(this);
        this.memoryProfileInputs = Object.fromEntries(Object.entries({
            preferred_language: 'memoryLanguage', answer_detail: 'memoryDetail',
            preferred_output_order: 'memoryOutputOrder', default_cluster: 'memoryCluster',
            default_environment: 'memoryEnvironment', default_namespace: 'memoryNamespace',
            responsible_services: 'memoryServices', response_preferences: 'memoryResponsePreferences',
        }).map(([field, id]) => [field, document.getElementById(id)]));

        // 初始化时检查是否需要居中
        this.checkAndSetCentered();
    }

    // 绑定事件监听器
    bindEvents() {
        this.bindMemoryProfileEvents();
        // 登录表单提交
        if (this.loginForm) {
            this.loginForm.addEventListener('submit', (e) => {
                e.preventDefault();
                const username = this.loginUsername ? this.loginUsername.value.trim() : '';
                const password = this.loginPassword ? this.loginPassword.value : '';
                this.handleLoginSubmit(username, password);
            });
        }

        // 快捷演示身份卡片点击登录
        if (this.quickRoleCards && this.quickRoleCards.length > 0) {
            this.quickRoleCards.forEach(card => {
                card.addEventListener('click', () => {
                    this.quickRoleCards.forEach(c => c.classList.remove('active'));
                    card.classList.add('active');
                    const username = card.dataset.username || 'alice';
                    const password = card.dataset.password || '123456';
                    if (this.loginUsername) this.loginUsername.value = username;
                    if (this.loginPassword) this.loginPassword.value = password;
                    this.handleLoginSubmit(username, password);
                });
            });
        }

        // 退出登录按钮
        if (this.logoutBtn) {
            this.logoutBtn.addEventListener('click', () => this.logout());
        }

        // 首屏排障场景快捷卡片一键提问
        const scenarioCards = document.querySelectorAll('.scenario-card[data-prompt]');
        scenarioCards.forEach(card => {
            card.addEventListener('click', () => {
                if (this.isStreaming) return;
                const prompt = card.dataset.prompt;
                if (prompt && this.messageInput) {
                    this.messageInput.value = prompt;
                    this.sendMessage();
                }
            });
        });

        // 新建对话
        if (this.newChatBtn) {
            this.newChatBtn.addEventListener('click', () => this.newChat());
        }

        // 模式选择下拉菜单
        if (this.modeSelectorBtn) {
            this.modeSelectorBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleModeDropdown();
            });
        }

        // 下拉菜单项点击
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            item.addEventListener('click', (e) => {
                const mode = item.getAttribute('data-mode');
                this.selectMode(mode);
                this.closeModeDropdown();
            });
        });

        // 点击外部关闭下拉菜单
        document.addEventListener('click', (e) => {
            if (!this.modeSelectorBtn.contains(e.target) &&
                !this.modeDropdown.contains(e.target)) {
                this.closeModeDropdown();
            }
        });

        // 发送消息
        if (this.sendButton) {
            this.sendButton.addEventListener('click', () => this.sendMessage());
        }

        if (this.messageInput) {
            this.messageInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
        }

        // 工具按钮和菜单
        if (this.toolsBtn) {
            this.toolsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleToolsMenu();
            });
        }

        // 工具菜单项点击事件
        if (this.uploadFileItem) {
            this.uploadFileItem.addEventListener('click', () => {
                this.openKnowledgeDialog();
                if (!this.isKnowledgeBusy) this.fileInput.click();
                this.closeToolsMenu();
            });
        }

        // 点击外部关闭工具菜单
        document.addEventListener('click', (e) => {
            if (this.toolsBtn && this.toolsMenu &&
                !this.toolsBtn.contains(e.target) &&
                !this.toolsMenu.contains(e.target)) {
                this.closeToolsMenu();
            }
        });

        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }
        this.knowledgeBtn.addEventListener('click', () => this.openKnowledgeDialog());
        this.closeKnowledgeBtn.addEventListener('click', () => this.knowledgeDialog.close());
        this.knowledgeUploadBtn.addEventListener('click', () => {
            if (!this.isKnowledgeBusy) this.fileInput.click();
        });
        this.reindexForm.addEventListener('submit', (event) => {
            event.preventDefault();
            this.reindexDirectory();
        });
        this.refreshToolsBtn.addEventListener('click', () => this.refreshTools());
    }

    async loadToolStatus() {
        if (!this.currentUser) return;
        if (this.isLoadingToolStatus) return;
        this.isLoadingToolStatus = true;
        try {
            const response = await this.authFetch(`${this.apiBaseUrl}/tools/status`, {
                cache: 'no-store', signal: AbortSignal.timeout(5000)
            });
            const payload = await response.json();
            if (!response.ok || !Array.isArray(payload.data?.servers)) {
                throw new Error('工具状态接口不可用');
            }
            const data = payload.data;
            const unavailable = data.servers.filter(server => server.status === 'unavailable');
            const checking = data.servers.some(server => server.status === 'checking');
            this.toolStatusSummary.textContent = unavailable.length
                ? `${unavailable.length} 项不可用` : checking ? '检测中' : '全部可用';
            this.toolStatusPanel.dataset.state = unavailable.length ? 'warning' : checking ? 'checking' : 'success';
            // 首次发现故障时展开，之后尊重用户手动收起的状态。
            if (unavailable.length && !this.hasShownToolFailure) {
                this.toolStatusPanel.open = true;
                this.hasShownToolFailure = true;
            }
            if (!unavailable.length) this.hasShownToolFailure = false;
            this.toolStatusList.replaceChildren();
            for (const server of data.servers) {
                const item = document.createElement('div');
                item.className = 'tool-service-status';
                const title = document.createElement('strong');
                const labels = { available: '可用', unavailable: '不可用', checking: '检测中' };
                title.textContent = `${server.label}：${labels[server.status] || '状态未知'}`;
                item.appendChild(title);
                const names = document.createElement('p');
                names.textContent = server.tools.length
                    ? `${server.status === 'available' ? '可用工具' : '受影响工具'}：${server.tools.join('、')}`
                    : '尚未获取工具列表';
                item.appendChild(names);
                const mode = document.createElement('p');
                mode.textContent = server.transport === 'stdio' ? '本地进程由框架自动管理' : '连接外部 MCP 服务';
                item.appendChild(mode);
                if (server.last_error) {
                    const error = document.createElement('p');
                    error.className = 'tool-service-error';
                    error.textContent = server.last_error;
                    item.appendChild(error);
                }
                this.toolStatusList.appendChild(item);
            }
            this.refreshToolsBtn.disabled = data.refreshing;
            this.toolStatusNotice.textContent = data.refreshing
                ? '正在后台检测，知识库问答可继续使用。'
                : '不可用服务会自动重试，也可点击重新检测。';
        } catch (error) {
            this.toolStatusSummary.textContent = '状态未知';
            this.toolStatusPanel.dataset.state = 'warning';
            this.toolStatusNotice.textContent = '无法获取工具状态，请检查主服务连接。';
            this.refreshToolsBtn.disabled = false;
        } finally {
            this.isLoadingToolStatus = false;
        }
    }

    async refreshTools() {
        this.refreshToolsBtn.disabled = true;
        try {
            const response = await this.authFetch(`${this.apiBaseUrl}/tools/refresh`, {
                method: 'POST', signal: AbortSignal.timeout(5000)
            });
            if (response.status !== 202) throw new Error('提交工具检测失败');
            this.toolStatusNotice.textContent = '已提交后台检测，稍后会自动更新。';
            await this.loadToolStatus();
        } catch (error) {
            this.toolStatusNotice.textContent = error.message;
        } finally {
            this.refreshToolsBtn.disabled = false;
        }
    }

    // 切换工具菜单显示/隐藏
    toggleToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭工具菜单
    closeToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 新建对话
    newChat() {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再新建对话', 'warning');
            return;
        }

        // 如果当前有对话内容，且不是从历史记录加载的，才保存为新的历史对话
        // 如果是从历史记录加载的，只需要更新该历史记录
        if (this.currentChatHistory.length > 0) {
            if (this.isCurrentChatFromHistory) {
                // 当前对话是从历史记录加载的，更新该历史记录
                this.updateCurrentChatHistory();
            } else {
                // 当前对话是新对话，保存为新的历史对话
                this.saveCurrentChat();
            }
        }

        // 停止所有进行中的操作
        this.isStreaming = false;

        // 清空输入框
        if (this.messageInput) {
            this.messageInput.value = '';
        }

        // 清空当前对话历史
        this.currentChatHistory = [];

        // 重置标记
        this.isCurrentChatFromHistory = false;

        // 清空聊天记录
        if (this.chatMessages) {
            this.chatMessages.innerHTML = '';
        }

        // 生成新的会话ID
        this.sessionId = this.generateSessionId();

        // 保持当前所选执行引擎模式并刷新UI
        this.updateUI();

        // 重新设置居中样式（确保对话框居中显示）
        this.checkAndSetCentered();

        // 确保容器有过渡动画
        if (this.chatContainer) {
            this.chatContainer.style.transition = 'all 0.5s ease';
        }

        // 更新历史对话列表
        this.renderChatHistory();
    }

    // 保存当前对话到历史记录（新建）
    saveCurrentChat() {
        if (this.currentChatHistory.length === 0) {
            return;
        }

        // 检查是否已存在相同ID的历史记录
        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex !== -1) {
            // 如果已存在，更新而不是新建
            this.updateCurrentChatHistory();
            return;
        }

        // 获取对话标题（使用第一条用户消息的前30个字符）
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        const title = firstUserMessage ?
            (firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : '')) :
            '新对话';

        const chatHistory = {
            id: this.sessionId,
            title: title,
            messages: [...this.currentChatHistory],
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString()
        };

        // 添加到历史记录列表的开头
        this.chatHistories.unshift(chatHistory);

        // 限制历史记录数量（最多保存50条）
        if (this.chatHistories.length > 50) {
            this.chatHistories = this.chatHistories.slice(0, 50);
        }

        // 保存到localStorage
        this.saveChatHistories();
    }

    // 更新当前对话的历史记录
    updateCurrentChatHistory() {
        if (this.currentChatHistory.length === 0) {
            return;
        }

        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex === -1) {
            // 如果不存在，调用保存方法
            this.saveCurrentChat();
            return;
        }

        // 更新现有的历史记录
        const history = this.chatHistories[existingIndex];
        history.messages = [...this.currentChatHistory];
        history.updatedAt = new Date().toISOString();

        // 如果标题需要更新（第一条消息改变了）
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        if (firstUserMessage) {
            const newTitle = firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : '');
            if (history.title !== newTitle) {
                history.title = newTitle;
            }
        }

        // 保存到localStorage
        this.saveChatHistories();
    }

    bindMemoryProfileEvents() {
        this.memoryBtn?.addEventListener('click', () => this.openMemoryDialog());
        this.closeMemoryBtn?.addEventListener('click', () => this.memoryDialog.close());
        this.memoryDialog?.addEventListener('close', () => {
            if (!this.memoryDialog.open) this.resetMemoryProfileView();
        });
        this.memoryProfileForm?.addEventListener('submit', event => {
            event.preventDefault();
            this.saveMemoryProfile();
        });
        this.memoryReloadBtn?.addEventListener('click', () => this.loadMemoryProfile());
        this.memoryClearBtn?.addEventListener('click', () => {
            if (this.memoryProfileBusy || !this.memoryProfile) return;
            for (const input of Object.values(this.memoryProfileInputs)) input.value = '';
            this.setMemoryProfileStatus('warning', '字段已清空，点击保存后才会生效。');
        });
    }

    resetMemoryProfileView() {
        this.incidentMemory?.reset();
        this.memoryJobs?.reset();
        this.invalidateMemoryProfile();
        if (this.memoryDialog?.open) this.memoryDialog.close();
        if (this.memoryProfileStatusText) this.memoryProfileStatusText.textContent = '尚未加载画像。';
        if (this.memoryProfileStatus) delete this.memoryProfileStatus.dataset.state;
    }

    invalidateMemoryProfile() {
        this.memoryProfileRequest = (this.memoryProfileRequest || 0) + 1;
        this.memoryProfileController?.abort();
        this.memoryProfileController = null;
        this.memoryProfile = null;
        this.memoryProfileConflict = false;
        this.memoryProfileForm?.reset();
        if (this.memoryProfileOwner) this.memoryProfileOwner.textContent = '';
        this.setMemoryProfileBusy(false);
    }

    setMemoryProfileBusy(busy) {
        this.memoryProfileBusy = busy;
        if (this.memoryProfileFields) this.memoryProfileFields.disabled = busy || !this.memoryProfile;
        if (this.memorySaveBtn) this.memorySaveBtn.disabled = busy || !this.memoryProfile || this.memoryProfileConflict;
        if (this.memoryClearBtn) this.memoryClearBtn.disabled = busy || !this.memoryProfile;
        if (this.memoryReloadBtn) this.memoryReloadBtn.disabled = busy;
    }

    setMemoryProfileStatus(state, message) {
        if (this.memoryProfileStatus) this.memoryProfileStatus.dataset.state = state;
        if (this.memoryProfileStatusText) this.memoryProfileStatusText.textContent = message;
    }

    async openMemoryDialog() {
        if (!this.currentUser) {
            this.showLoginOverlay();
            return;
        }
        if (!this.memoryDialog.open) {
            // A close event can still be queued when the dialog is opened again.
            this.resetMemoryProfileView();
            this.memoryDialog.showModal();
        }
        await this.loadMemoryProfile();
    }

    applyMemoryProfile(data) {
        if (!data || !Number.isInteger(data.revision) || data.revision < 0 || !data.profile) {
            throw new Error('画像数据无效，请重试');
        }
        this.memoryProfile = data;
        this.memoryProfileConflict = false;
        for (const [field, input] of Object.entries(this.memoryProfileInputs)) {
            const value = data.profile[field];
            input.value = Array.isArray(value) ? value.join(field === 'response_preferences' ? '\n' : ', ') : (value || '');
        }
        this.memoryUseEnabled.checked = data.use_enabled;
        if (this.memoryAutoExtract) this.memoryAutoExtract.checked = data.auto_extract_enabled;
        if (this.memoryAutoHelp) this.memoryAutoHelp.textContent = data.automatic_available
            ? '对话结束后，符合条件的内容会生成待审核建议。'
            : '服务端自动提取开关或额度尚未开启。';
        this.memoryProfileOwner.textContent = `${this.currentUser.display_name || this.currentUser.username || ''} · 版本 ${data.revision}`;
    }

    memoryRequestIsCurrent(request, version) {
        return request === this.memoryProfileRequest && version === this.authVersion && this.memoryDialog?.open;
    }

    async loadMemoryProfile() {
        if (!this.currentUser || !this.memoryDialog?.open || this.memoryProfileBusy) return;
        const request = ++this.memoryProfileRequest;
        const version = this.authVersion;
        this.memoryProfileController = new AbortController();
        this.setMemoryProfileBusy(true);
        this.setMemoryProfileStatus('running', '正在加载当前账号的画像…');
        try {
            const response = await this.authFetch('/api/memory/profile', {signal: this.memoryProfileController.signal});
            if (!this.memoryRequestIsCurrent(request, version)) return;
            if (!response.ok) throw new Error('画像加载失败，请重新加载');
            const result = await response.json();
            if (!this.memoryRequestIsCurrent(request, version)) return;
            this.applyMemoryProfile(result.data);
            this.setMemoryProfileStatus('success', result.data.runtime_connected
                ? '已加载。开启使用后，下一轮聊天会参考已保存的偏好。'
                : '已加载。服务端记忆开关关闭，画像尚未用于生成回复。');
        } catch (error) {
            if (this.memoryRequestIsCurrent(request, version)) this.setMemoryProfileStatus('error', error.message);
        } finally {
            if (this.memoryRequestIsCurrent(request, version)) this.setMemoryProfileBusy(false);
        }
    }

    async saveMemoryProfile() {
        if (!this.currentUser || !this.memoryProfile || this.memoryProfileBusy || this.memoryProfileConflict || !this.memoryDialog?.open) return;
        const request = ++this.memoryProfileRequest;
        const version = this.authVersion;
        const changes = {};
        for (const [field, input] of Object.entries(this.memoryProfileInputs)) {
            const separator = field === 'response_preferences' ? /\r?\n/ : field === 'responsible_services' ? /[,，\n]+/ : null;
            const value = separator
                ? [...new Set(input.value.split(separator).map(item => item.trim()).filter(Boolean))]
                : (input.value.trim() || null);
            const original = this.memoryProfile.profile[field] ?? (separator ? [] : null);
            if (JSON.stringify(value) !== JSON.stringify(original)) changes[field] = value;
        }
        const patch = {expected_revision: this.memoryProfile.revision, changes};
        if (this.memoryUseEnabled.checked !== this.memoryProfile.use_enabled) patch.use_enabled = this.memoryUseEnabled.checked;
        if (this.memoryAutoExtract && this.memoryAutoExtract.checked !== this.memoryProfile.auto_extract_enabled) {
            patch.auto_extract_enabled = this.memoryAutoExtract.checked;
        }
        this.memoryProfileController = new AbortController();
        this.setMemoryProfileBusy(true);
        this.setMemoryProfileStatus('running', '正在保存画像…');
        try {
            const response = await this.authFetch('/api/memory/profile', {
                method: 'PATCH', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(patch), signal: this.memoryProfileController.signal,
            });
            if (!this.memoryRequestIsCurrent(request, version)) return;
            if (response.status === 409) {
                this.memoryProfileConflict = true;
                this.setMemoryProfileStatus('warning', '画像已在其他页面修改。本次输入仍保留；请先确认输入，再点击重新加载后编辑，避免覆盖新版本。');
                return;
            }
            const result = await response.json();
            if (!this.memoryRequestIsCurrent(request, version)) return;
            if (!response.ok) {
                const detail = result.detail;
                const labels = {
                    preferred_language: '语言代码', answer_detail: '回答详细程度',
                    preferred_output_order: '输出顺序', default_cluster: '默认集群',
                    default_environment: '默认环境', default_namespace: '默认命名空间',
                    responsible_services: '负责的服务', response_preferences: '自然语言回答偏好',
                };
                const fields = Array.isArray(detail) ? [...new Set(detail.map(item => labels[item.loc?.[2]] || '画像字段'))] : [];
                const message = fields.length ? `请检查${fields.join('、')}，格式或长度不符合要求。` : detail;
                throw new Error(typeof message === 'string' ? message : '保存失败，请重试');
            }
            this.applyMemoryProfile(result.data);
            this.setMemoryProfileStatus('success', result.data.runtime_connected
                ? '画像已保存。变更会在下一轮生效，环境设置只用于提示核实目标。'
                : '画像已保存。服务端记忆开关关闭。');
        } catch (error) {
            if (this.memoryRequestIsCurrent(request, version)) this.setMemoryProfileStatus('error', error.message);
        } finally {
            if (this.memoryRequestIsCurrent(request, version)) this.setMemoryProfileBusy(false);
        }
    }

    // 构建携带 Bearer Token 的请求头
    authHeaders(extraHeaders = {}) {
        const headers = new Headers(extraHeaders);
        if (this.accessToken) headers.set('Authorization', `Bearer ${this.accessToken}`);
        return headers;
    }

    async authFetch(url, options = {}) {
        const version = this.authVersion;
        const controller = new AbortController();
        this.pendingAuthRequests.add(controller);
        const onAbort = () => controller.abort();
        options.signal?.addEventListener('abort', onAbort, {once: true});
        if (options.signal?.aborted) controller.abort();
        const cleanup = () => {
            this.pendingAuthRequests.delete(controller);
            options.signal?.removeEventListener('abort', onAbort);
        };
        const checkCurrent = () => {
            if (version !== this.authVersion) throw new Error('登录身份已变化，已忽略旧请求');
        };
        try {
            const response = await fetch(url, {...options, headers: this.authHeaders(options.headers), signal: controller.signal});
            checkCurrent();
            if (response.status === 401) {
                cleanup();
                this.handleUnauthorized();
                return response;
            }
            const originalJson = response.json.bind(response);
            response.json = async () => {
                const data = await originalJson();
                checkCurrent();
                return data;
            };
            if (response.headers?.get('content-type')?.includes('text/event-stream') && response.body) {
                const getReader = response.body.getReader.bind(response.body);
                response.body.getReader = (...args) => {
                    const reader = getReader(...args);
                    return {
                        read: async () => {
                            checkCurrent();
                            const result = await reader.read();
                            checkCurrent();
                            if (result.done) cleanup();
                            return result;
                        },
                        cancel: async reason => {try {await reader.cancel(reason);} finally {cleanup();}},
                        releaseLock: () => {try {reader.releaseLock();} finally {cleanup();}},
                    };
                };
            } else cleanup();
            return response;
        } catch (error) {
            cleanup();
            throw error;
        }
    }

    clearAuthState() {
        this.authVersion++;
        for (const controller of this.pendingAuthRequests) controller.abort();
        this.pendingAuthRequests.clear();
        this.accessToken = '';
        this.currentUser = null;
        this.resetMemoryProfileView();
        this.currentChatHistory = [];
        this.chatHistories = [];
        this.isCurrentChatFromHistory = false;
        if (this.chatMessages) this.chatMessages.innerHTML = '';
        if (this.loginPassword) this.loginPassword.value = '';
        if (this.knowledgeDialog?.open) this.knowledgeDialog.close();
        if (this.knowledgeResultDetails) this.knowledgeResultDetails.textContent = '';
        this.sessionId = this.generateSessionId();
        this.renderChatHistory();
        this.checkAndSetCentered();
    }

    handleUnauthorized() {
        this.clearAuthState();
        localStorage.removeItem('oncall_access_token');
        this.showLoginOverlay();
    }

    showLoginOverlay() {
        if (this.loginOverlay) {
            this.loginOverlay.classList.remove('hidden');
        }
        if (this.loginError) {
            this.loginError.hidden = true;
            this.loginError.textContent = '';
        }
    }

    hideLoginOverlay() {
        if (this.loginOverlay) {
            this.loginOverlay.classList.add('hidden');
        }
    }

    applyUserProfile(user) {
        if (!user) return;
        this.currentUser = user;
        const avatarEl = document.getElementById('userAvatar');
        const nameEl = document.getElementById('userName');
        const badgeEl = document.getElementById('userRoleBadge');
        const deptEl = document.getElementById('userDept');
        if (avatarEl) avatarEl.textContent = user.avatar || '👩‍💻';
        if (nameEl) nameEl.textContent = (user.display_name || user.username || '').split('(')[0].trim();
        if (badgeEl) badgeEl.textContent = (user.role || 'sre').toUpperCase();
        if (deptEl) deptEl.textContent = user.department || '基础设施与稳定性运维部';
    }

    // 初始化登录态检查（前端路由守卫）
    async initAuth() {
        const version = this.authVersion;
        try {
            // HttpOnly Cookie can restore login even if localStorage was cleared.
            const resp = await this.authFetch('/api/auth/me');
            if (!resp.ok) {
                if (resp.status !== 401) this.handleUnauthorized();
                return;
            }
            const res = await resp.json();
            if (res.data) {
                this.applyUserProfile(res.data);
                this.hideLoginOverlay();
                await this.loadChatHistories();
                await this.loadToolStatus();
            } else {
                this.handleUnauthorized();
            }
        } catch (e) {
            if (version !== this.authVersion) return;
            console.error('校验登录状态失败:', e);
            this.showLoginOverlay();
        }
    }

    // 提交账号密码登录
    async handleLoginSubmit(username, password) {
        if (this.authBusy) return;
        if (this.isStreaming) {
            this.showNotification('请等待当前请求完成后再切换账号', 'warning');
            return;
        }
        if (!username || !password) {
            if (this.loginError) {
                this.loginError.textContent = '请输入运维账号与密码';
                this.loginError.hidden = false;
            }
            return;
        }
        this.authBusy = true;
        if (this.loginSubmitBtn) this.loginSubmitBtn.disabled = true;
        if (this.loginError) this.loginError.hidden = true;

        try {
            const resp = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            const res = await resp.json();
            if (!resp.ok || res.code !== 200) {
                const msg = res.detail || res.message || '用户名或密码错误';
                if (this.loginError) {
                    this.loginError.textContent = msg;
                    this.loginError.hidden = false;
                }
                return;
            }

            const { access_token, user } = res.data;
            this.clearAuthState();
            this.accessToken = access_token;
            localStorage.setItem('oncall_access_token', access_token);
            this.applyUserProfile(user);

            // 重置并加载该租户专属会话列表
            this.currentChatHistory = [];
            this.chatHistories = [];
            this.isCurrentChatFromHistory = false;
            this.newChat();
            this.hideLoginOverlay();
            await this.loadChatHistories();
            await this.loadToolStatus();
            this.showNotification(`已登录：${user.display_name}`, 'success');
        } catch (e) {
            console.error('登录请求异常:', e);
            if (this.loginError) {
                this.loginError.textContent = '无法连接认证服务，请确认后端已启动';
                this.loginError.hidden = false;
            }
        } finally {
            this.authBusy = false;
            if (this.loginSubmitBtn) this.loginSubmitBtn.disabled = false;
        }
    }

    // 退出当前登录账号（返回登录页）
    async logout() {
        if (this.authBusy) return;
        if (this.isStreaming) {
            this.showNotification('请等待当前排障回复完成后再退出登录', 'warning');
            return;
        }
        this.authBusy = true;
        try {
            const response = await fetch('/api/auth/logout', {method: 'POST', headers: this.authHeaders()});
            if (!response.ok) throw new Error('退出登录失败，请重试');
            this.clearAuthState();
            localStorage.removeItem('oncall_access_token');
            this.showLoginOverlay();
        } catch (error) {
            this.showNotification(error.message || '退出登录失败，请重试', 'error');
        } finally {
            this.authBusy = false;
        }
    }

    // 加载历史会话列表（从 SQLite 持久化服务端按租户 Token 加载）
    async loadChatHistories() {
        if (!this.currentUser) return [];
        try {
            const response = await this.authFetch('/api/sessions');
            if (response.ok) {
                const res = await response.json();
                const sessions = res.data.sessions || [];
                this.chatHistories = sessions.map(s => ({
                    id: s.session_id,
                    title: s.title,
                    updated_at: s.updated_at,
                    message_count: s.message_count,
                    preview: s.last_message_preview
                }));
                this.renderChatHistory();
                return this.chatHistories;
            }
        } catch (e) {
            console.error('从服务端加载历史会话失败:', e);
        }
        return [];
    }

    // 保存历史对话列表（兼容空实现）
    saveChatHistories() {
        // 后端 SQLite 自动持久化
    }

    // 渲染历史对话列表
    renderChatHistory() {
        if (!this.chatHistoryList) {
            return;
        }

        this.chatHistoryList.innerHTML = '';

        if (!this.chatHistories || this.chatHistories.length === 0) {
            this.chatHistoryList.innerHTML = '<div style="padding: 12px; font-size: 12px; color: #80868b; text-align: center;">暂无历史排障记录</div>';
            return;
        }

        this.chatHistories.forEach((history) => {
            const historyItem = document.createElement('div');
            const isActive = history.id === this.sessionId;
            historyItem.className = `history-item ${isActive ? 'active' : ''}`;
            historyItem.dataset.historyId = history.id;

            historyItem.innerHTML = `
                <div class="history-item-content">
                    <span class="history-item-title" title="${this.escapeHtml(history.title)}">${this.escapeHtml(history.title)}</span>
                </div>
                <button class="history-item-delete" data-history-id="${history.id}" title="删除会话，同时撤回关联事故">
                    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M18 6L6 18M6 6L18 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                    </svg>
                </button>
            `;

            // 点击历史项加载对话
            historyItem.addEventListener('click', (e) => {
                if (!e.target.closest('.history-item-delete')) {
                    this.loadChatHistory(history.id);
                }
            });

            // 删除历史对话
            const deleteBtn = historyItem.querySelector('.history-item-delete');
            deleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.deleteChatHistory(history.id);
            });

            this.chatHistoryList.appendChild(historyItem);
        });
    }

    // 加载历史对话
    async loadChatHistory(historyId) {
        if (this.isStreaming) return;
        try {
            const response = await this.authFetch(`/api/sessions/${encodeURIComponent(historyId)}`);
            if (!response.ok) throw new Error('会话不存在或加载失败');
            const result = await response.json();
            this.sessionId = historyId;
            this.isCurrentChatFromHistory = true;
            this.currentChatHistory = [];
            this.chatMessages.innerHTML = '';
            for (const msg of result.data.messages || []) {
                const type = msg.role === 'user' ? 'user' : 'assistant';
                this.currentChatHistory.push({type, content: msg.content, timestamp: msg.timestamp});
                const message = this.addMessage(type, msg.content, false, false);
                if (type === 'assistant') this.renderExecutionTrace(message, msg.execution_trace);
            }
            this.checkAndSetCentered();
            this.renderChatHistory();
        } catch (error) {
            this.showNotification(error.message, 'error');
        }
    }

    // 删除历史对话
    async deleteChatHistory(historyId) {
        if (this.isStreaming) return;
        try {
            const response = await this.authFetch(`/api/sessions/${encodeURIComponent(historyId)}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                throw new Error('删除会话失败');
            }

            this.chatHistories = this.chatHistories.filter(h => h.id !== historyId);
            this.renderChatHistory();

            // 如果删除的是当前对话，清空当前对话
            if (this.sessionId === historyId) {
                this.currentChatHistory = [];
                if (this.chatMessages) {
                    this.chatMessages.innerHTML = '';
                }
                this.sessionId = this.generateSessionId();
                this.checkAndSetCentered();
            }

            this.showNotification('会话及其关联的事故经验已清理，个人画像保留。', 'success');
        } catch (error) {
            console.error('删除历史对话失败:', error);
            this.showNotification('删除失败: ' + error.message, 'error');
        }
    }

    // 切换模式下拉菜单
    toggleModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭模式下拉菜单
    closeModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 选择模式
    selectMode(mode) {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再切换模式', 'warning');
            return;
        }

        this.currentMode = mode;
        this.updateUI();

        const modeNames = {
            'quick': '快速直出',
            'stream': '流式推演'
        };

        this.showNotification(`已切换到${modeNames[mode]}模式`, 'info');
    }

    // 更新UI
    updateUI() {
        // 更新模式选择器显示
        if (this.currentModeText) {
            const modeNames = {
                'quick': '快速直出',
                'stream': '流式推演'
            };
            this.currentModeText.textContent = modeNames[this.currentMode] || '流式推演';
        }

        // 更新下拉菜单选中状态
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            const mode = item.getAttribute('data-mode');
            if (mode === this.currentMode) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });

        // 更新发送按钮状态
        if (this.sendButton) {
            this.sendButton.disabled = this.isStreaming;
        }

        // 更新输入框状态
        if (this.messageInput) {
            this.messageInput.disabled = this.isStreaming;
            this.messageInput.placeholder = '输入告警信息、报错日志、K8s 命令或运维排障问题 (Enter 发送)...';
        }
    }

    // 生成随机会话ID
    generateSessionId() {
        if (typeof crypto.randomUUID === 'function') {
            return 'session_' + crypto.randomUUID();
        }
        // getRandomValues is also available on plain HTTP LAN origins.
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        return 'session_' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    }

    // 发送消息
    async sendMessage() {
        let message = '';
        if (this.messageInput) {
            message = this.messageInput.value.trim();
        }

        if (!message) {
            this.showNotification('请输入消息内容', 'warning');
            return;
        }

        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成', 'warning');
            return;
        }

        // 显示用户消息
        this.addMessage('user', message);

        // 清空输入框
        if (this.messageInput) {
            this.messageInput.value = '';
        }

        // 设置发送状态
        this.isStreaming = true;
        this.updateUI();

        try {
            if (this.currentMode === 'quick') {
                await this.sendQuickMessage(message);
            } else if (this.currentMode === 'stream') {
                await this.sendStreamMessage(message);
            }
        } catch (error) {
            console.error('发送消息失败:', error);
            if (this.currentUser) this.addMessage('assistant', '抱歉，发送消息时出现错误：' + error.message);
        } finally {
            this.isStreaming = false;
            this.updateUI();

            // 如果当前对话是从历史记录加载的，更新历史记录
            if (this.isCurrentChatFromHistory && this.currentChatHistory.length > 0) {
                this.updateCurrentChatHistory();
                this.renderChatHistory(); // 更新历史对话列表显示
            }
        }
    }

    // 发送快速消息（普通对话）
    async sendQuickMessage(message) {
        // 添加等待提示消息
        const loadingMessage = this.addLoadingMessage('正在思考...');

        try {
            const response = await this.authFetch(`${this.apiBaseUrl}/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.data?.errorMessage || data.detail || data.message || `HTTP错误: ${response.status}`);
            }

            // 移除等待提示消息
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }

            // 统一响应格式：检查 data.code 或 data.message 判断请求是否成功
            if (data.code === 200 || data.message === 'success') {
                // data.data 是 ChatResponse 对象
                const chatResponse = data.data;

                if (chatResponse && chatResponse.success) {
                    // 成功：添加实际响应消息（即使 answer 为空也显示）
                    const answer = chatResponse.answer || '（无回复内容）';
                    const msgElement = this.addMessage('assistant', answer);
                    this.renderMemoryBadge(msgElement, chatResponse.memory);
                    this.notifyMemoryJob(chatResponse.memory_job_id);

                    const routeTrace = chatResponse.route ? [{type: 'route', data: chatResponse.route}] : [];
                    this.renderExecutionTrace(msgElement, chatResponse.execution_trace ?? routeTrace);
                } else if (chatResponse && chatResponse.errorMessage) {
                    // 业务错误
                    throw new Error(chatResponse.errorMessage);
                } else {
                    // 兜底：尝试显示任何可用内容
                    const fallbackAnswer = chatResponse?.answer || chatResponse?.errorMessage || '服务返回了空内容';
                    this.addMessage('assistant', fallbackAnswer);
                }
                // 刷新侧边栏持久化历史
                this.loadChatHistories();
            } else {
                // HTTP 成功但业务失败
                throw new Error(data.data?.errorMessage || data.message || '请求失败');
            }
        } catch (error) {
            // 出错时也要移除等待提示消息
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }
            throw error;
        }
    }

    // 发送流式消息
    async sendStreamMessage(message) {
        try {
            const response = await this.authFetch(`${this.apiBaseUrl}/chat_stream`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            // 创建助手消息元素
            const assistantMessageElement = this.addMessage('assistant', '', true);
            let fullResponse = '';
            const streamStartTime = Date.now();
            let thoughtContainer = null;
            let thoughtStepCount = 0;

            // 处理流式响应
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let currentEvent = '';

            try {
                while (true) {
                    const { done, value } = await reader.read();

                    if (done) {
                        // 流结束，自动折叠思考面板并最终渲染
                        if (thoughtContainer) {
                            this.completeThoughtAccordion(thoughtContainer, thoughtStepCount, streamStartTime);
                        }
                        this.handleStreamComplete(assistantMessageElement, fullResponse);
                        break;
                    }

                    // 解码数据并添加到缓冲区
                    buffer += decoder.decode(value, { stream: true });

                    // 按行分割处理
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.trim() === '') continue;

                        // 解析SSE格式
                        if (line.startsWith('id:')) {
                            continue;
                        } else if (line.startsWith('event:')) {
                            currentEvent = line.substring(6).trim();
                            continue;
                        } else if (line.startsWith('data:')) {
                            const rawData = line.substring(5).trim();

                            // 兼容旧格式 [DONE] 标记
                            if (rawData === '[DONE]') {
                                if (thoughtContainer) {
                                    this.completeThoughtAccordion(thoughtContainer, thoughtStepCount, streamStartTime);
                                }
                                this.handleStreamComplete(assistantMessageElement, fullResponse);
                                return;
                            }

                            // 处理 SSE 数据
                            let sseMessage;
                            try {
                                sseMessage = JSON.parse(rawData);

                                if (sseMessage && typeof sseMessage.type === 'string') {
                                    if (EXECUTION_EVENT_TYPES.has(sseMessage.type)) {
                                        if (!thoughtContainer) thoughtContainer = this.createThoughtContainer(assistantMessageElement);
                                        thoughtStepCount += this.renderExecutionEvent(thoughtContainer, sseMessage);
                                    }
                                    else if (sseMessage.type === 'memory_recall') {
                                        this.renderMemoryBadge(assistantMessageElement, sseMessage.data);
                                    }
                                    // 4. 正文流式渲染
                                    else if (sseMessage.type === 'content') {
                                        const content = sseMessage.data || '';
                                        fullResponse += content;

                                        if (assistantMessageElement) {
                                            const messageContent = assistantMessageElement.querySelector('.message-content');
                                            messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                            this.highlightCodeBlocks(messageContent);
                                            this.scrollToBottom();
                                        }
                                    }
                                    // 5. 完成事件 -> 自动折叠思考面板，仅露总结报告
                                    else if (sseMessage.type === 'done') {
                                        this.notifyMemoryJob(sseMessage.data?.memory_job_id);
                                        if (typeof sseMessage.data?.answer === 'string') {
                                            fullResponse = sseMessage.data.answer;
                                        }
                                        if (thoughtContainer) {
                                            this.completeThoughtAccordion(thoughtContainer, thoughtStepCount, streamStartTime);
                                        }
                                        this.handleStreamComplete(assistantMessageElement, fullResponse);
                                        return;
                                    }
                                    // 6. 异常事件
                                    else if (sseMessage.type === 'error') {
                                        if (thoughtContainer) {
                                            this.addThoughtStep(thoughtContainer, 'badge-tool', '❌ 异常中断', String(sseMessage.data));
                                            this.updateThoughtSummary(thoughtContainer, '执行异常中断', true);
                                        }
                                        if (assistantMessageElement) {
                                            const messageContent = assistantMessageElement.querySelector('.message-content');
                                            messageContent.innerHTML = this.renderMarkdown('错误: ' + (sseMessage.data || '未知错误'));
                                        }
                                        return;
                                    }
                                } else {
                                    // 兼容非 JSON 纯文本流
                                    fullResponse += rawData;
                                    if (assistantMessageElement) {
                                        const messageContent = assistantMessageElement.querySelector('.message-content');
                                        messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                        this.highlightCodeBlocks(messageContent);
                                        this.scrollToBottom();
                                    }
                                }
                            } catch (e) {
                                if (EXECUTION_EVENT_TYPES.has(sseMessage?.type)) throw e;
                                if (rawData === '') {
                                    fullResponse += '\n';
                                } else {
                                    fullResponse += rawData;
                                }

                                if (assistantMessageElement) {
                                    const messageContent = assistantMessageElement.querySelector('.message-content');
                                    messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                    this.highlightCodeBlocks(messageContent);
                                    this.scrollToBottom();
                                }
                            }
                        }
                    }
                }
            } finally {
                // 确保在流关闭或中断时也触发优雅折叠
                if (thoughtContainer && thoughtContainer.hasAttribute('open') && fullResponse) {
                    this.completeThoughtAccordion(thoughtContainer, thoughtStepCount, streamStartTime);
                }
                reader.releaseLock();
            }
        } catch (error) {
            throw error;
        }
    }

    // ========================================================
    // Codex / DeepSeek R1 风格：Agent 思考轨迹与折叠组件管理
    // ========================================================
    createThoughtContainer(assistantMessageElement) {
        if (!assistantMessageElement) return null;
        const wrapper = assistantMessageElement.querySelector('.message-content-wrapper');
        if (!wrapper) return null;
        let container = wrapper.querySelector('.agent-thought-container');
        if (container) return container;

        container = document.createElement('details');
        container.className = 'agent-thought-container';
        container.setAttribute('open', 'true');

        container.innerHTML = `
            <summary class="agent-thought-summary">
                <div class="summary-left">
                    <span class="thought-spinner"></span>
                    <span class="summary-title">任务主管决策中...</span>
                </div>
                <svg class="summary-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="9 18 15 12 9 6"></polyline>
                </svg>
            </summary>
            <div class="agent-thought-steps"></div>
        `;

        const content = wrapper.querySelector('.message-content');
        if (content) {
            wrapper.insertBefore(container, content);
        } else {
            wrapper.appendChild(container);
        }
        return container;
    }

    notifyMemoryJob(jobId) {
        if (jobId) this.showNotification('记忆提取已排队，可在「长期记忆 → 任务状态」查看。', 'success');
    }

    renderMemoryBadge(message, memory) {
        if (!message || !memory) return;
        const wrapper = message.querySelector('.message-content-wrapper');
        if (!wrapper) return;
        const statuses = {
            recalled: '已参考事故经验', profile_only: '已参考个人画像',
            degraded: '部分记忆暂不可用', no_match: '本轮未使用事故经验',
        };
        const reasons = {
            scope_unavailable: '未确定适用环境',
            timeout: '读取超时', memory_unavailable: '记忆读取失败',
            lexical_limited: '词法检索受限', lexical_unavailable: '词法检索暂不可用',
            vector_budget_disabled: '向量调用未启用', vector_unavailable: '向量检索暂不可用',
        };
        const title = statuses[memory.status];
        if (!title || (memory.status === 'no_match' && !memory.reason)) return;
        const entries = Array.isArray(memory.incidents) ? memory.incidents : [];
        const row = document.createElement('div');
        row.className = 'thought-step-item memory-recall-badge';
        const badge = document.createElement('span');
        badge.className = 'step-badge badge-step';
        badge.textContent = title;
        const details = document.createElement('span');
        details.className = 'step-text';
        details.textContent = [reasons[memory.reason], ...entries.map(item => `${item.title}（版本 ${item.revision}）`)]
            .filter(Boolean).join(' · ');
        row.append(badge, details);
        wrapper.querySelector('.memory-recall-badge')?.remove();
        wrapper.prepend(row);
    }

    addThoughtStep(container, badgeClass, badgeLabel, stepText, latencyText = '') {
        if (!container) return;
        const stepsList = container.querySelector('.agent-thought-steps');
        if (!stepsList) return;

        const stepItem = document.createElement('div');
        stepItem.className = 'thought-step-item';
        stepItem.innerHTML = `
            <span class="step-badge ${badgeClass}">${badgeLabel}</span>
            <div class="step-text">${stepText}</div>
            ${latencyText ? `<span class="step-latency">${latencyText}</span>` : ''}
        `;
        stepsList.appendChild(stepItem);

        // 自动平滑滚动到最新步骤
        stepsList.scrollTop = stepsList.scrollHeight;
    }

    renderExecutionTrace(message, events) {
        if (!events?.length) return;
        const container = this.createThoughtContainer(message);
        let count = 0;
        for (const event of events) count += this.renderExecutionEvent(container, event);
        this.completeThoughtAccordion(container, count);
    }

    renderExecutionEvent(container, event) {
        const data = event.data || {};
        switch (event.type) {
            case 'route': {
                const sources = {
                    rule_fast_pass: '规则快速放行', semantic_router: '语义向量聚类',
                    llm_supervisor: 'Supervisor LLM 结构化仲裁',
                };
                const intents = {
                    knowledge_qa: 'ReAct 知识与工具智能体', aiops_diagnosis: 'AIOps 线上排障工作流',
                    general_chat: '通用轻量闲聊',
                };
                const source = sources[data.source] || '意图判定';
                const intent = intents[data.intent] || '业务链路';
                const reason = data.reason ? ` · ${this.escapeHtml(data.reason)}` : '';
                const latency = Number.isFinite(data.latency_ms) ? `${data.latency_ms}ms` : '';
                this.addThoughtStep(container, 'badge-route', '🎯 任务路由',
                    `<strong>${source}</strong> → 分流至【${intent}】<span style="color:#64748b;">${reason}</span>`, latency);
                this.updateThoughtSummary(container, `任务主管已决策 → 正在执行 ${intent}...`);
                break;
            }
            case 'aiops_progress':
                return this.renderAIOpsProgress(container, data);
            case 'tool_call': {
                const tool = data.tool || 'system_tool';
                const status = data.status || '执行中';
                const query = data.query ? ` · <code>${this.escapeHtml(data.query)}</code>` : '';
                const details = data.details ? ` <span style="color:#64748b;">(${this.escapeHtml(data.details)})</span>` : '';

                if (tool === 'search_web') {
                    this.addThoughtStep(container, 'badge-step', '🌐 联网搜索',
                        `通过 Tavily 检索公网资料${query}${details}`);
                    this.updateThoughtSummary(container, '正在进行公网信息检索…');
                } else if (tool === 'retrieve_knowledge') {
                    this.addThoughtStep(container, 'badge-step', '🔍 知识检索',
                        `检索内部知识库与排障 SOP${query}${details}`);
                    this.updateThoughtSummary(container, '正在检索内部知识库…');
                } else if (tool === 'get_current_time') {
                    this.addThoughtStep(container, 'badge-step', '⏱️ 时间锚定',
                        `获取权威系统当前时间基准${details}`);
                    this.updateThoughtSummary(container, '正在核对当前时间…');
                } else {
                    this.addThoughtStep(container, 'badge-tool', '⚡ 工具调用',
                        `调用运维工具: <code>${this.escapeHtml(tool)}</code> (${this.escapeHtml(status)})${details}`);
                    this.updateThoughtSummary(container, `正在执行工具 ${this.escapeHtml(tool)}…`);
                }
                break;
            }
            case 'search_results': {
                const count = data.count ? `已召回 ${data.count} 条相关内部文档分片` : '父子文档切片与 SOP 召回完成';
                const docs = Array.isArray(data.docs) && data.docs.length > 0
                    ? `<div style="margin-top:4px; font-size:12px; color:#475569;">${data.docs.map(d => `• <code>${this.escapeHtml(d)}</code>`).join('<br>')}</div>`
                    : '';
                this.addThoughtStep(container, 'badge-step', '📚 知识召回', `${count}${docs}`);
                break;
            }
            case 'compaction': {
                const isMacro = data.stage === 'macro';
                const tokens = data.details?.final_tokens;
                this.addThoughtStep(container, isMacro ? 'badge-route' : 'badge-tool',
                    isMacro ? '🧠 宏观上下文归纳' : '✂️ 工具输出剪枝',
                    this.escapeHtml(data.message || '正在优化上下文...'), Number.isFinite(tokens) ? `${tokens} Tokens` : '');
                this.updateThoughtSummary(container, isMacro ? '正在压缩长上下文…' : '正在剪枝工具输出…');
                break;
            }
            case 'debug':
                this.addThoughtStep(container, 'badge-step', '⚙️ 步骤流转',
                    `流转节点: <code>${this.escapeHtml(event.node || 'workflow')}</code> (${this.escapeHtml(event.message_type || 'info')})`);
                break;
            default:
                return 0;
        }
        return 1;
    }

    renderAIOpsProgress(container, event) {
        if (event.type === 'plan' && Array.isArray(event.plan)) {
            const items = event.plan.map(step => `<li>${this.escapeHtml(step)}</li>`).join('');
            const title = `查看执行计划（${event.plan.length} 项）`;
            this.addThoughtStep(container, 'badge-step', '执行计划',
                `<details class="aiops-plan-details"><summary>${title}</summary><ol>${items}</ol></details>`);
            this.updateThoughtSummary(container, `排障计划已制定 · ${event.plan.length} 项`);
            return 1;
        }
        if (event.type === 'step_complete') {
            this.addThoughtStep(container, 'badge-done', '步骤结束', this.escapeHtml(event.current_step));
            this.updateThoughtSummary(container, `排障执行中 · 剩余 ${event.remaining_steps} 项`);
            return 1;
        }
        if (event.type === 'status' && event.stage === 'replanner') {
            this.updateThoughtSummary(container, '正在评估下一步…');
        }
        return 0;
    }

    updateThoughtSummary(container, titleText, isDone = false) {
        if (!container) return;
        const titleEl = container.querySelector('.summary-title');
        if (titleEl) titleEl.textContent = titleText;
        const spinner = container.querySelector('.thought-spinner');
        if (isDone && spinner) {
            spinner.remove();
        }
    }

    completeThoughtAccordion(container, totalSteps, startTime) {
        if (!container) return;
        const duration = startTime ? ` · 耗时 ${((Date.now() - startTime) / 1000).toFixed(1)}s` : '';
        const title = `✅ 分析与排障完成 (${totalSteps} 条记录${duration} · 点击展开执行明细)`;
        this.updateThoughtSummary(container, title, true);
        // 核心：任务完成后自动折叠，保持主界面清爽
        container.removeAttribute('open');
    }

    // 添加消息到聊天界面
    addMessage(type, content, isStreaming = false, saveToHistory = true) {
        // 检查是否是第一条消息，如果是则移除居中样式
        const isFirstMessage = this.chatMessages && this.chatMessages.querySelectorAll('.message').length === 0;

        // 保存消息到当前对话历史（如果不是流式消息且需要保存）
        if (!isStreaming && saveToHistory && content) {
            this.currentChatHistory.push({
                type: type,
                content: content,
                timestamp: new Date().toISOString()
            });
        }

        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}${isStreaming ? ' streaming' : ''}`;

        // 如果是assistant消息，添加头像图标
        if (type === 'assistant') {
            const messageAvatar = document.createElement('div');
            messageAvatar.className = 'message-avatar';
            messageAvatar.innerHTML = `
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="white"/>
                </svg>
            `;
            messageDiv.appendChild(messageAvatar);
        }

        // 创建消息内容包装器
        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';

        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';

        // 如果是assistant消息且不是流式消息，使用Markdown渲染
        if (type === 'assistant' && !isStreaming) {
            messageContent.innerHTML = this.renderMarkdown(content);
            // 高亮代码块
            this.highlightCodeBlocks(messageContent);
        } else {
            // 用户消息或流式消息使用纯文本
            messageContent.textContent = content;
        }

        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);

            // 如果是第一条消息，移除居中样式并添加动画
            if (isFirstMessage && this.chatContainer) {
                this.chatContainer.classList.remove('centered');
                // 添加动画类
                this.chatContainer.style.transition = 'all 0.5s ease';
            }

            this.scrollToBottom();
        }

        return messageDiv;
    }

    // 添加带加载动画的消息
    addLoadingMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant';

        // 添加头像图标
        const messageAvatar = document.createElement('div');
        messageAvatar.className = 'message-avatar';
        messageAvatar.innerHTML = `
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="white"/>
            </svg>
        `;
        messageDiv.appendChild(messageAvatar);

        // 创建消息内容包装器
        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';

        const messageContent = document.createElement('div');
        messageContent.className = 'message-content loading-message-content';

        // 创建文本和动画容器
        const textSpan = document.createElement('span');
        textSpan.textContent = content;

        // 创建旋转动画图标
        const loadingIcon = document.createElement('span');
        loadingIcon.className = 'loading-spinner-icon';
        loadingIcon.innerHTML = `
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z" fill="currentColor" opacity="0.2"/>
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10c1.54 0 3-.36 4.28-1l-1.5-2.6C13.64 19.62 12.84 20 12 20c-4.41 0-8-3.59-8-8s3.59-8 8-8c.84 0 1.64.38 2.18 1l1.5-2.6C13 2.36 12.54 2 12 2z" fill="currentColor"/>
            </svg>
        `;

        messageContent.appendChild(textSpan);
        messageContent.appendChild(loadingIcon);
        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);

            // 如果是第一条消息，移除居中样式
            const isFirstMessage = this.chatMessages.querySelectorAll('.message').length === 1;
            if (isFirstMessage && this.chatContainer) {
                this.chatContainer.classList.remove('centered');
                this.chatContainer.style.transition = 'all 0.5s ease';
            }

            this.scrollToBottom();
        }

        return messageDiv;
    }

    // 检查并设置居中样式
    checkAndSetCentered() {
        if (this.chatMessages && this.chatContainer) {
            const hasMessages = this.chatMessages.querySelectorAll('.message').length > 0;
            if (!hasMessages) {
                this.chatContainer.classList.add('centered');
            } else {
                this.chatContainer.classList.remove('centered');
            }
        }
    }

    // 滚动到底部
    scrollToBottom() {
        if (this.chatMessages) {
            this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
        }
    }

    // 处理流式传输完成
    handleStreamComplete(assistantMessageElement, fullResponse) {
        if (assistantMessageElement) {
            assistantMessageElement.classList.remove('streaming');
            const messageContent = assistantMessageElement.querySelector('.message-content');
            if (messageContent) {
                messageContent.innerHTML = this.renderMarkdown(fullResponse);
                // 高亮代码块
                this.highlightCodeBlocks(messageContent);
            }
        }
        // 保存流式消息到历史记录
        if (fullResponse) {
            this.currentChatHistory.push({
                type: 'assistant',
                content: fullResponse,
                timestamp: new Date().toISOString()
            });
            // 无论新会话还是旧会话，通知侧边栏刷新持久化历史列表
            this.loadChatHistories();
        }
    }

    // 显示通知
    showNotification(message, type = 'info') {
        // 创建通知元素
        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.textContent = message;
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 20px;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            z-index: 10000;
            animation: slideIn 0.3s ease;
            max-width: 300px;
        `;

        // 根据类型设置颜色（Google Material Design配色）
        const colors = {
            info: '#1a73e8',
            success: '#34a853',
            warning: '#fbbc04',
            error: '#ea4335'
        };
        notification.style.backgroundColor = colors[type] || colors.info;

        // 添加到页面
        document.body.appendChild(notification);

        // 3秒后自动移除
        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => {
                if (notification.parentNode) {
                    notification.parentNode.removeChild(notification);
                }
            }, 300);
        }, 3000);
    }

    openKnowledgeDialog() {
        if (!this.knowledgeDialog.open) this.knowledgeDialog.showModal();
    }

    // 上传与重建共用任务状态，不占用聊天的流式状态。
    setKnowledgeBusy(busy) {
        this.isKnowledgeBusy = busy;
        this.knowledgeUploadBtn.disabled = busy;
        this.fileInput.disabled = busy;
        this.reindexBtn.disabled = busy;
        this.indexDirectoryInput.disabled = busy;
        this.knowledgeResult.setAttribute('aria-busy', String(busy));
    }

    showKnowledgeResult(type, title, lines = [], failedFiles = {}) {
        this.knowledgeResult.dataset.state = type;
        this.knowledgeResultTitle.textContent = title;
        this.knowledgeResultDetails.replaceChildren();
        for (const line of lines) {
            const paragraph = document.createElement('p');
            paragraph.textContent = line;
            this.knowledgeResultDetails.appendChild(paragraph);
        }
        const failures = Object.entries(failedFiles);
        if (failures.length) {
            const details = document.createElement('details');
            details.open = true;
            const summary = document.createElement('summary');
            summary.textContent = `失败文件及原因（${failures.length}）`;
            details.appendChild(summary);
            const list = document.createElement('ul');
            for (const [path, reason] of failures) {
                const item = document.createElement('li');
                item.textContent = `${path}：${reason}`;
                list.appendChild(item);
            }
            details.appendChild(list);
            this.knowledgeResultDetails.appendChild(details);
        }
    }

    async knowledgeRequest(path, options = {}) {
        let response;
        try {
            response = await this.authFetch(`${this.apiBaseUrl}${path}`, options);
        } catch (error) {
            throw new Error('无法连接后端或连接已中断，当前无法确认操作结果。请检查服务和网络，并查看 LangSmith 中的执行记录。');
        }
        let payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error(`后端未返回有效 JSON（HTTP ${response.status}），无法确认操作结果。请检查服务状态。`);
        }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new Error(`后端响应格式异常（HTTP ${response.status}），无法确认操作结果。`);
        }
        if (!response.ok || payload.code !== 200) {
            const detail = payload.detail;
            const message = typeof detail === 'string' ? detail
                : Array.isArray(detail) ? detail.map(item => item.msg).join('；')
                : payload.message || `请求失败（HTTP ${response.status}）`;
            throw new Error(message);
        }
        if (!payload.data || typeof payload.data !== 'object' || Array.isArray(payload.data)) {
            throw new Error('后端响应缺少操作结果，无法确认是否完成。');
        }
        return payload.data;
    }

    // 处理文件选择；立即重置，允许失败后重新选择同一文件。
    handleFileSelect(event) {
        const file = event.target.files[0];
        event.target.value = '';
        if (file) this.uploadFile(file);
    }

    // 验证文件类型
    validateFileType(file) {
        const fileName = file.name.toLowerCase();
        const allowedExtensions = ['.txt', '.md'];
        return allowedExtensions.some(ext => fileName.endsWith(ext));
    }

    // 上传文件到知识库
    async uploadFile(file) {
        if (this.isKnowledgeBusy) return;
        this.openKnowledgeDialog();
        if (!this.validateFileType(file)) {
            this.showKnowledgeResult('error', '文件格式不支持', ['只支持 .txt 和 .md 文件。']);
            return;
        }
        if (file.size === 0 || file.size > 10 * 1024 * 1024) {
            this.showKnowledgeResult('error', '文件大小不符合要求', ['文件不能为空，且不能超过 10 MB。']);
            return;
        }
        this.setKnowledgeBusy(true);
        this.showKnowledgeResult('running', '正在上传并建立父子索引…', [
            `文件：${file.name}（${this.formatFileSize(file.size)}）`,
            '正在加载、切分、向量化并保存索引。完成前请保留当前页面；关闭面板不会取消任务。'
        ]);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const result = await this.knowledgeRequest('/upload', { method: 'POST', body: formData });
            if (!Number.isInteger(result.index?.parent_count) || result.index.parent_count < 1) {
                throw new Error('后端未返回有效的父文档数量，无法确认索引是否完成。');
            }
            this.showKnowledgeResult('success', '上传并建立索引成功', [
                `文件：${result.filename}`,
                `生成父文档：${result.index.parent_count} 个；关联子文档已向量化入库。`,
                '现在可以关闭面板，在对话中提问。'
            ]);
            this.showNotification('文档索引已完成', 'success');
        } catch (error) {
            this.showKnowledgeResult('error', '上传或索引未完成确认', [error.message]);
            this.showNotification('文档处理失败，请查看知识库面板', 'error');
        } finally {
            this.setKnowledgeBusy(false);
        }
    }

    async reindexDirectory() {
        if (this.isKnowledgeBusy) return;
        const directory = this.indexDirectoryInput.value.trim();
        if (!directory) {
            this.showKnowledgeResult('error', '请填写文档目录', ['例如：data/corpus/processed、uploads 或 aiops-docs。']);
            this.indexDirectoryInput.focus();
            return;
        }
        this.indexDirectoryInput.value = directory;
        this.setKnowledgeBusy(true);
        this.showKnowledgeResult('running', '正在重建目录索引…', [
            `目录：${directory}`,
            '文件较多时需要等待。完成前请保留当前页面；关闭面板不会取消任务。'
        ]);
        try {
            const params = new URLSearchParams({ directory_path: directory });
            const result = await this.knowledgeRequest(`/index_directory?${params}`, { method: 'POST' });
            const counts = [result.total_files, result.success_count, result.fail_count];
            if (!counts.every(value => Number.isInteger(value) && value >= 0)
                || result.total_files !== result.success_count + result.fail_count) {
                throw new Error('后端返回的文件统计不完整，无法确认重建结果。');
            }
            const lines = [
                `目录：${result.directory_path || directory}`,
                `文件总数：${result.total_files}；成功：${result.success_count}；失败：${result.fail_count}`
            ];
            if (Number.isFinite(result.duration_ms)) {
                lines.push(`耗时：${(result.duration_ms / 1000).toFixed(1)} 秒`);
            }
            if (result.error_message) lines.push(result.error_message);
            let type;
            let title;
            if (result.success !== true || result.fail_count > 0 || result.error_message) {
                type = result.success_count > 0 ? 'warning' : 'error';
                title = result.success_count > 0 ? '部分文件索引失败' : '目录索引失败';
            } else if (result.total_files === 0) {
                type = 'warning';
                title = '目录中没有可索引的文档';
                lines.push('未更新索引。请确认目录中存在 .md 或 .txt 文件。');
            } else {
                type = 'success';
                title = '目录索引重建完成';
                lines.push('所列文件的父子索引已更新，可以返回对话提问。');
            }
            this.showKnowledgeResult(type, title, lines, result.failed_files || {});
            this.showNotification(title, type);
        } catch (error) {
            this.showKnowledgeResult('error', '目录重建未完成确认', [error.message]);
            this.showNotification('重建失败，请查看知识库面板', 'error');
        } finally {
            this.setKnowledgeBusy(false);
        }
    }

    // 格式化文件大小
    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    // 发送智能运维请求（SSE 流式模式）
    async sendAIOpsRequest(loadingMessageElement) {
        try {
            const response = await this.authFetch(`${this.apiBaseUrl}/aiops`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    session_id: this.sessionId
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            let fullResponse = '';

            // 处理 SSE 流式响应
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let currentEvent = 'message'; // 默认事件类型为 message

            try {
                while (true) {
                    const { done, value } = await reader.read();

                    if (done) {
                        // 流结束，更新最终内容
                        if (fullResponse) {
                            console.log('AI Ops 流结束，更新最终内容，长度:', fullResponse.length);
                            this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                        }
                        break;
                    }

                    // 解码数据并添加到缓冲区
                    buffer += decoder.decode(value, { stream: true });

                    // 按行分割处理
                    const lines = buffer.split('\n');
                    // 保留最后一行（可能不完整）
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.trim() === '') continue;


                        // 解析 SSE 格式
                        if (line.startsWith('id:')) {
                            continue;
                        } else if (line.startsWith('event:')) {
                            currentEvent = line.substring(6).trim();
                            console.log('[AI Ops SSE] 事件类型:', currentEvent);
                            continue;
                        } else if (line.startsWith('data:')) {
                            const rawData = line.substring(5).trim();

                            // 解析可能包含多个JSON对象的数据
                            const processJsonMessages = (data) => {
                                const jsonPattern = /\{"type"\s*:\s*"[^"]+"\s*,\s*"data"\s*:\s*(?:"[^"]*"|null)\}/g;
                                const matches = data.match(jsonPattern);

                                if (matches && matches.length > 0) {
                                    console.log('[AI Ops SSE] 匹配到', matches.length, '个JSON对象');
                                    for (const jsonStr of matches) {
                                        try {
                                            const sseMessage = JSON.parse(jsonStr);
                                            if (sseMessage.type === 'content') {
                                                fullResponse += sseMessage.data || '';
                                            } else if (sseMessage.type === 'plan') {
                                                // 处理计划创建事件
                                                const planText = `\n\n## 📋 执行计划\n${sseMessage.message}\n\n`;
                                                fullResponse += planText;
                                            } else if (sseMessage.type === 'step_complete') {
                                                // 处理步骤完成事件
                                                const stepText = `\n✅ ${sseMessage.message}\n`;
                                                fullResponse += stepText;
                                            } else if (sseMessage.type === 'status') {
                                                // 处理状态更新事件
                                                const statusText = `\n⏳ ${sseMessage.message}\n`;
                                                fullResponse += statusText;
                                            } else if (sseMessage.type === 'report') {
                                                // 处理最终报告事件 - 流式输出
                                                console.log('AI Ops 最终报告生成');
                                                const reportText = `\n\n## 🎯 诊断报告\n\n${sseMessage.report || ''}\n`;
                                                fullResponse += reportText;
                                            } else if (sseMessage.type === 'complete') {
                                                // 处理完成事件
                                                console.log('AI Ops 诊断完成');
                                                if (sseMessage.response) {
                                                    fullResponse += `\n\n${sseMessage.response}`;
                                                }
                                                this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                return true;
                                            } else if (sseMessage.type === 'done') {
                                                console.log('AI Ops 流完成，最终内容长度:', fullResponse.length);
                                                this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                return true;
                                            } else if (sseMessage.type === 'error') {
                                                throw new Error(sseMessage.data || sseMessage.message || '智能运维分析失败');
                                            }
                                        } catch (e) {
                                            if (e.message.includes('智能运维')) throw e;
                                            console.log('[AI Ops SSE] 单个JSON解析失败:', jsonStr);
                                        }
                                    }
                                    if (loadingMessageElement) {
                                        this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                    }
                                    return false;
                                }
                                return null;
                            };

                            const result = processJsonMessages(rawData);
                            if (result === true) {
                                return; // 流结束
                            } else if (result === null) {
                                // 没有匹配到多个JSON，尝试单个JSON解析
                                try {
                                    const sseMessage = JSON.parse(rawData);
                                    if (sseMessage && sseMessage.type) {
                                        if (sseMessage.type === 'memory_recall') {
                                            this.renderMemoryBadge(loadingMessageElement, sseMessage.data);
                                        } else if (sseMessage.type === 'content') {
                                            fullResponse += sseMessage.data || '';
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        } else if (sseMessage.type === 'plan') {
                                            // 处理计划创建事件
                                            const planText = `\n\n## 📋 执行计划\n${sseMessage.message}\n\n`;
                                            fullResponse += planText;
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        } else if (sseMessage.type === 'step_complete') {
                                            // 处理步骤完成事件
                                            const stepText = `\n✅ ${sseMessage.message}\n`;
                                            fullResponse += stepText;
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        } else if (sseMessage.type === 'status') {
                                            // 处理状态更新事件
                                            const statusText = `\n⏳ ${sseMessage.message}\n`;
                                            fullResponse += statusText;
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        } else if (sseMessage.type === 'report') {
                                            // 处理最终报告事件 - 这是关键！
                                            console.log('AI Ops 最终报告生成，流式输出中...');
                                            const reportText = `\n\n## 🎯 诊断报告\n\n${sseMessage.report || ''}\n`;
                                            fullResponse += reportText;
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        } else if (sseMessage.type === 'complete') {
                                            // 处理完成事件
                                            console.log('AI Ops 诊断完成，最终内容长度:', fullResponse.length);
                                            this.notifyMemoryJob(sseMessage.memory_job_id);
                                            const report = sseMessage.diagnosis?.report || sseMessage.response;
                                            if (report) fullResponse = report;
                                            // 使用最终的完整内容更新消息
                                            this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                            return;
                                        } else if (sseMessage.type === 'done') {
                                            console.log('AI Ops 流完成，最终内容长度:', fullResponse.length);
                                            this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                            return;
                                        } else if (sseMessage.type === 'error') {
                                            throw new Error(sseMessage.data || sseMessage.message || '智能运维分析失败');
                                        }
                                    } else {
                                        fullResponse += rawData;
                                        if (loadingMessageElement) {
                                            this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                        }
                                    }
                                } catch (e) {
                                    if (e.message.includes('智能运维')) throw e;
                                    // 非 JSON 格式，直接追加原始数据
                                    fullResponse += rawData;
                                    if (loadingMessageElement) {
                                        this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                    }
                                }
                            }
                        }
                    }
                }
            } finally {
                reader.releaseLock();
            }
        } catch (error) {
            throw error;
        }
    }

    // 更新智能运维流式内容（实时显示）
    updateAIOpsStreamContent(messageElement, content) {
        if (!messageElement) return;

        // 添加 aiops-message 类
        messageElement.classList.add('aiops-message');

        const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
        if (messageContentWrapper) {
            let messageContent = messageContentWrapper.querySelector('.message-content');
            if (!messageContent) {
                messageContent = document.createElement('div');
                messageContent.className = 'message-content';
                messageContentWrapper.appendChild(messageContent);
            }
            // 流式显示时使用纯文本
            messageContent.textContent = content;
            this.scrollToBottom();
        }
    }

    // 更新智能运维消息（带折叠详情）
    updateAIOpsMessage(messageElement, response, details) {
        console.log('updateAIOpsMessage 被调用');
        console.log('messageElement:', messageElement);
        console.log('response:', response);
        console.log('response length:', response ? response.length : 0);
        console.log('details:', details);

        if (!messageElement) {
            // 如果没有传入消息元素，则创建新消息
            console.log('messageElement 为空，创建新消息');
            return this.addAIOpsMessage(response, details);
        }

        // 添加aiops-message类
        messageElement.classList.add('aiops-message');

        // 获取消息内容包装器
        const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
        if (!messageContentWrapper) {
            console.error('未找到 message-content-wrapper');
            return;
        }

        // 清空现有内容（保留消息内容容器）
        const messageContent = messageContentWrapper.querySelector('.message-content');
        if (!messageContent) {
            console.error('未找到 message-content');
            return;
        }

        // 移除加载动画相关的类和内容
        messageContent.classList.remove('loading-message-content');
        messageContent.textContent = '';

        // 移除加载图标（如果存在）
        const loadingIcon = messageContent.querySelector('.loading-spinner-icon');
        if (loadingIcon) {
            loadingIcon.remove();
        }

        // 详情部分（可折叠）- 先显示
        if (details && details.length > 0) {
            // 检查是否已存在详情容器
            let detailsContainer = messageElement.querySelector('.aiops-details');
            if (!detailsContainer) {
                detailsContainer = document.createElement('div');
                detailsContainer.className = 'aiops-details';
                messageContentWrapper.insertBefore(detailsContainer, messageContent);
            } else {
                // 清空现有详情
                detailsContainer.innerHTML = '';
            }

            const detailsToggle = document.createElement('div');
            detailsToggle.className = 'details-toggle';
            detailsToggle.innerHTML = `
                <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>查看详细步骤 (${details.length}条)</span>
            `;

            const detailsContent = document.createElement('div');
            detailsContent.className = 'details-content';

            details.forEach((detail, index) => {
                const detailItem = document.createElement('div');
                detailItem.className = 'detail-item';
                detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${this.escapeHtml(detail)}`;
                detailsContent.appendChild(detailItem);
            });

            // 点击切换折叠状态
            detailsToggle.addEventListener('click', () => {
                detailsContent.classList.toggle('expanded');
                detailsToggle.classList.toggle('expanded');
            });

            detailsContainer.appendChild(detailsToggle);
            detailsContainer.appendChild(detailsContent);
        }

        // 更新主要响应内容（使用Markdown渲染）
        console.log('开始渲染 Markdown');
        const renderedHtml = this.renderMarkdown(response);
        console.log('Markdown 渲染完成，HTML 长度:', renderedHtml ? renderedHtml.length : 0);
        messageContent.innerHTML = renderedHtml;
        console.log('innerHTML 已设置');
        // 高亮代码块
        this.highlightCodeBlocks(messageContent);
        console.log('代码块高亮完成');

        // 保存到历史记录
        this.currentChatHistory.push({
            type: 'assistant',
            content: response,
            timestamp: new Date().toISOString()
        });

        this.scrollToBottom();
        return messageElement;
    }

    // 添加智能运维消息（带折叠详情）- 保留用于兼容性
    addAIOpsMessage(response, details) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant aiops-message';

        // 添加头像图标
        const messageAvatar = document.createElement('div');
        messageAvatar.className = 'message-avatar';
        messageAvatar.innerHTML = `
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="white"/>
            </svg>
        `;
        messageDiv.appendChild(messageAvatar);

        // 创建消息内容包装器
        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';

        // 详情部分（可折叠）- 先显示
        if (details && details.length > 0) {
            const detailsContainer = document.createElement('div');
            detailsContainer.className = 'aiops-details';

            const detailsToggle = document.createElement('div');
            detailsToggle.className = 'details-toggle';
            detailsToggle.innerHTML = `
                <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>查看详细步骤 (${details.length}条)</span>
            `;

            const detailsContent = document.createElement('div');
            detailsContent.className = 'details-content';

            details.forEach((detail, index) => {
                const detailItem = document.createElement('div');
                detailItem.className = 'detail-item';
                detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${this.escapeHtml(detail)}`;
                detailsContent.appendChild(detailItem);
            });

            // 点击切换折叠状态
            detailsToggle.addEventListener('click', () => {
                detailsContent.classList.toggle('expanded');
                detailsToggle.classList.toggle('expanded');
            });

            detailsContainer.appendChild(detailsToggle);
            detailsContainer.appendChild(detailsContent);
            messageContentWrapper.appendChild(detailsContainer);
        }

        // 主要响应内容 - 后显示（使用Markdown渲染）
        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';
        messageContent.innerHTML = this.renderMarkdown(response);
        // 高亮代码块
        this.highlightCodeBlocks(messageContent);
        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            this.scrollToBottom();
        }

        return messageDiv;
    }

    // HTML转义
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // 触发智能运维（点击智能运维按钮时直接调用）
    async triggerAIOps() {
        if (this.isStreaming) {
            this.showNotification('请等待当前操作完成', 'warning');
            return;
        }

        // 新建对话
        this.newChat();

        // 添加"分析中..."的消息（带旋转动画）
        const loadingMessage = this.addLoadingMessage('分析中...');
        this.currentAIOpsMessage = loadingMessage; // 保存消息引用用于后续更新

        // 设置发送状态
        this.isStreaming = true;
        this.updateUI();

        try {
            await this.sendAIOpsRequest(loadingMessage);
        } catch (error) {
            console.error('智能运维分析失败:', error);
            // 更新消息为错误信息
            if (loadingMessage) {
                const messageContent = loadingMessage.querySelector('.message-content');
                if (messageContent) {
                    messageContent.textContent = '抱歉，智能运维分析时出现错误：' + error.message;
                }
            }
        } finally {
            this.isStreaming = false;
            this.currentAIOpsMessage = null;
            this.updateUI();
        }
    }

    // 显示/隐藏加载遮罩层
    showLoadingOverlay(show) {
        if (this.loadingOverlay) {
            if (show) {
                this.loadingOverlay.style.display = 'flex';
                // 更新文字为智能运维
                const loadingText = this.loadingOverlay.querySelector('.loading-text');
                const loadingSubtext = this.loadingOverlay.querySelector('.loading-subtext');
                if (loadingText) loadingText.textContent = '智能运维分析中，请稍候...';
                if (loadingSubtext) loadingSubtext.textContent = '后端正在处理，请耐心等待';
                // 防止页面滚动
                document.body.style.overflow = 'hidden';
            } else {
                this.loadingOverlay.style.display = 'none';
                // 恢复页面滚动
                document.body.style.overflow = '';
            }
        }
    }

}

// 添加CSS动画
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }

    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// 初始化应用
document.addEventListener('DOMContentLoaded', () => {
    new OnCallAegntApp();
});
