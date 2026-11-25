/**
 * MCP 探针与工具中心前端管理模块
 * 支持服务列表实时监控、添加探针、JSON 极速配置与工具全景 Schema 检索
 */

(function () {
    'use strict';

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    const McpManager = {
        dialog: null,
        currentTab: 'servers',
        serversData: [],
        toolsData: [],
        rawJson: '',

        init() {
            this.dialog = document.getElementById('mcpConfigDialog');
            if (!this.dialog) return;

            const openBtn = document.getElementById('mcpConfigBtn');
            const closeBtn = document.getElementById('closeMcpConfigBtn');

            if (openBtn) {
                openBtn.addEventListener('click', () => this.open());
            }
            if (closeBtn) {
                closeBtn.addEventListener('click', () => this.close());
            }

            // Tab 切换
            const tabs = [
                { id: 'mcpServersTab', pane: 'mcpServersPane', name: 'servers' },
                { id: 'mcpAddTab', pane: 'mcpAddPane', name: 'add' },
                { id: 'mcpJsonTab', pane: 'mcpJsonPane', name: 'json' },
                { id: 'mcpToolsTab', pane: 'mcpToolsPane', name: 'tools' },
            ];

            tabs.forEach(tab => {
                const btn = document.getElementById(tab.id);
                if (btn) {
                    btn.addEventListener('click', () => this.switchTab(tab.name));
                }
            });

            // 传输协议切换联动表单
            const transportSelect = document.getElementById('mcpTransport');
            const stdioFields = document.getElementById('mcpStdioFields');
            const httpFields = document.getElementById('mcpHttpFields');
            if (transportSelect && stdioFields && httpFields) {
                transportSelect.addEventListener('change', () => {
                    if (transportSelect.value === 'stdio') {
                        stdioFields.hidden = false;
                        httpFields.hidden = true;
                    } else {
                        stdioFields.hidden = true;
                        httpFields.hidden = false;
                    }
                });
            }

            // 表单提交：添加新服务
            const addForm = document.getElementById('mcpAddForm');
            if (addForm) {
                addForm.addEventListener('submit', (e) => this.handleAddServer(e));
            }
            const addCancelBtn = document.getElementById('mcpAddCancelBtn');
            if (addCancelBtn) {
                addCancelBtn.addEventListener('click', () => this.switchTab('servers'));
            }

            // JSON 编辑器操作
            const saveJsonBtn = document.getElementById('mcpSaveJsonBtn');
            if (saveJsonBtn) {
                saveJsonBtn.addEventListener('click', () => this.handleSaveJson());
            }
            const formatJsonBtn = document.getElementById('mcpFormatJsonBtn');
            if (formatJsonBtn) {
                formatJsonBtn.addEventListener('click', () => this.handleFormatJson());
            }
            const resetJsonBtn = document.getElementById('mcpResetJsonBtn');
            if (resetJsonBtn) {
                resetJsonBtn.addEventListener('click', () => this.handleResetJson());
            }

            // 立即刷新所有探测
            const refreshAllBtn = document.getElementById('mcpRefreshAllBtn');
            if (refreshAllBtn) {
                refreshAllBtn.addEventListener('click', () => this.handleRefreshAll());
            }

            // 工具搜索过滤
            const searchInput = document.getElementById('mcpToolSearchInput');
            if (searchInput) {
                searchInput.addEventListener('input', () => this.renderToolsList(searchInput.value.trim().toLowerCase()));
            }
        },

        async fetchApi(url, options = {}) {
            if (typeof window.authFetch === 'function') {
                return await window.authFetch(url, options);
            }
            return await fetch(url, options);
        },

        open() {
            if (!this.dialog) return;
            if (typeof this.dialog.showModal === 'function') {
                this.dialog.showModal();
            } else {
                this.dialog.setAttribute('open', '');
            }
            this.switchTab('servers');
            this.loadData();
        },

        close() {
            if (!this.dialog) return;
            if (typeof this.dialog.close === 'function') {
                this.dialog.close();
            } else {
                this.dialog.removeAttribute('open');
            }
        },

        switchTab(tabName) {
            this.currentTab = tabName;
            const tabMap = {
                servers: { btn: 'mcpServersTab', pane: 'mcpServersPane' },
                add: { btn: 'mcpAddTab', pane: 'mcpAddPane' },
                json: { btn: 'mcpJsonTab', pane: 'mcpJsonPane' },
                tools: { btn: 'mcpToolsTab', pane: 'mcpToolsPane' },
            };

            Object.entries(tabMap).forEach(([name, { btn, pane }]) => {
                const b = document.getElementById(btn);
                const p = document.getElementById(pane);
                if (b) {
                    b.setAttribute('aria-pressed', name === tabName ? 'true' : 'false');
                    b.classList.toggle('active', name === tabName);
                }
                if (p) {
                    p.hidden = name !== tabName;
                }
            });

            if (tabName === 'tools') {
                this.renderToolsList();
            }
        },

        async loadData() {
            try {
                let statusData = null;
                const res = await this.fetchApi('/api/tools/config');
                if (res.ok) {
                    const data = await res.json();
                    if (data.code === 200 && data.data) {
                        this.rawJson = data.data.raw || JSON.stringify(data.data.parsed || {}, null, 2);
                        statusData = data.data.status || {};
                    }
                }

                // 兜底降级：如果 /api/tools/config 接口 404（例如后端服务未重启），自动使用已有的 /api/tools/status
                if (!statusData) {
                    const fallbackRes = await this.fetchApi('/api/tools/status');
                    if (fallbackRes.ok) {
                        const fallbackData = await fallbackRes.json();
                        statusData = fallbackData.data || {};
                    }
                }

                if (statusData) {
                    this.serversData = statusData.servers || [];

                    // 抽取聚合所有工具
                    const tools = [];
                    this.serversData.forEach(server => {
                        const details = server.tool_details || [];
                        if (details.length > 0) {
                            details.forEach(tool => {
                                tools.push({
                                    ...tool,
                                    serverName: server.name,
                                    serverLabel: server.label || server.name,
                                    status: server.status
                                });
                            });
                        } else {
                            // 降级：如果后端尚未热重载 tool_details，从工具名列表生成基本项
                            (server.tools || []).forEach(toolName => {
                                tools.push({
                                    name: toolName,
                                    description: `属于探针服务 ${server.label || server.name} 的已就绪工具`,
                                    args: {},
                                    serverName: server.name,
                                    serverLabel: server.label || server.name,
                                    status: server.status
                                });
                            });
                        }
                    });
                    this.toolsData = tools;

                    this.renderServersList();
                    this.renderJsonEditor();
                    this.renderToolsList();
                }
            } catch (err) {
                console.error('加载 MCP 配置失败:', err);
            }
        },

        renderServersList() {
            const listEl = document.getElementById('mcpServersList');
            if (!listEl) return;

            if (!this.serversData || this.serversData.length === 0) {
                listEl.innerHTML = '<p class="mcp-helper-text" style="padding: 16px; text-align: center;">尚未检测到任何已挂载的 MCP 探针</p>';
                return;
            }

            const isBuiltin = (name) => name === 'cls' || name === 'monitor';

            listEl.innerHTML = this.serversData.map(server => {
                const statusClass = server.status || 'checking';
                const statusLabel = statusClass === 'available' ? '就绪' : statusClass === 'unavailable' ? '不可用' : '探测中';
                const toolsCount = (server.tools || []).length;
                const toolPills = (server.tools || []).map(t => `<span class="mcp-tool-pill">🔨 ${escapeHtml(t)}</span>`).join('');

                return `
                    <div class="mcp-server-card">
                        <div class="mcp-card-header">
                            <div class="mcp-card-title-group">
                                <span class="mcp-status-dot ${statusClass}" title="${statusLabel}"></span>
                                <span class="mcp-server-name">${escapeHtml(server.label || server.name)}</span>
                                <span class="mcp-badge">${escapeHtml(server.name)}</span>
                                <span class="mcp-badge" style="background: rgba(148, 163, 184, 0.15); color: var(--text-secondary);">${escapeHtml(server.transport || 'stdio')}</span>
                                ${isBuiltin(server.name) ? '<span class="mcp-badge" style="background: rgba(245, 158, 11, 0.12); color: #b45309;">内置服务</span>' : ''}
                            </div>
                            <div class="mcp-card-actions">
                                <span style="font-size: 12px; color: var(--text-muted); margin-right: 4px;">${toolsCount} 个工具可用</span>
                                ${!isBuiltin(server.name) ? `
                                    <button class="mcp-btn-sm mcp-btn-danger" type="button" onclick="window.McpManager.handleDeleteServer('${escapeHtml(server.name)}')">移除</button>
                                ` : ''}
                            </div>
                        </div>
                        ${server.last_error ? `
                            <div style="font-size: 12px; color: #ef4444; background: rgba(239, 68, 68, 0.08); padding: 6px 10px; border-radius: var(--radius-sm);">
                                错误详情: ${escapeHtml(server.last_error)}
                            </div>
                        ` : ''}
                        ${toolsCount > 0 ? `
                            <div class="mcp-tools-tags">
                                ${toolPills}
                            </div>
                        ` : ''}
                    </div>
                `;
            }).join('');
        },

        renderJsonEditor() {
            const editor = document.getElementById('mcpJsonEditor');
            if (editor) {
                editor.value = this.rawJson;
            }
        },

        renderToolsList(filterQuery = '') {
            const listEl = document.getElementById('mcpToolsList');
            if (!listEl) return;

            let filtered = this.toolsData;
            if (filterQuery) {
                filtered = filtered.filter(t => 
                    t.name.toLowerCase().includes(filterQuery) || 
                    (t.description && t.description.toLowerCase().includes(filterQuery)) ||
                    t.serverName.toLowerCase().includes(filterQuery)
                );
            }

            if (!filtered || filtered.length === 0) {
                listEl.innerHTML = `<p class="mcp-helper-text" style="padding: 16px; text-align: center;">${filterQuery ? '未找到匹配的工具' : '暂无可用工具'}</p>`;
                return;
            }

            listEl.innerHTML = filtered.map(tool => {
                const schema = tool.args || {};
                const properties = schema.properties || {};
                const requiredList = Array.isArray(schema.required) ? schema.required : [];

                const paramRows = Object.entries(properties).map(([paramName, prop]) => {
                    const isReq = requiredList.includes(paramName);
                    const typeStr = prop.type || 'any';
                    const descStr = prop.description || '无详细描述';
                    const defStr = prop.default !== undefined ? ` (默认: ${JSON.stringify(prop.default)})` : '';
                    return `
                        <tr>
                            <td><code>${escapeHtml(paramName)}</code> ${isReq ? '<span class="mcp-required-star" title="必填参数">*</span>' : ''}</td>
                            <td><span class="mcp-badge">${escapeHtml(typeStr)}</span></td>
                            <td>${escapeHtml(descStr)}${escapeHtml(defStr)}</td>
                        </tr>
                    `;
                }).join('');

                return `
                    <div class="mcp-tool-card">
                        <div class="mcp-tool-title-row">
                            <span class="mcp-tool-title">🔨 ${escapeHtml(tool.name)}</span>
                            <span class="mcp-badge">${escapeHtml(tool.serverLabel)}</span>
                        </div>
                        <div class="mcp-tool-desc">${escapeHtml(tool.description || '暂无工具描述')}</div>
                        ${paramRows ? `
                            <table class="mcp-params-table">
                                <thead>
                                    <tr>
                                        <th style="width: 25%;">参数名</th>
                                        <th style="width: 15%;">类型</th>
                                        <th>参数说明与默认值</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${paramRows}
                                </tbody>
                            </table>
                        ` : '<div style="font-size: 12px; color: var(--text-muted); font-style: italic;">该工具无需任何输入参数</div>'}
                    </div>
                `;
            }).join('');
        },

        async handleAddServer(e) {
            e.preventDefault();
            const name = document.getElementById('mcpServerName')?.value.trim();
            const transport = document.getElementById('mcpTransport')?.value || 'stdio';

            if (!name) return;

            const serverPayload = { transport };
            if (transport === 'stdio') {
                const command = document.getElementById('mcpCommand')?.value.trim() || 'python';
                const argsRaw = document.getElementById('mcpArgs')?.value || '';
                const args = argsRaw.split('\n').map(s => s.trim()).filter(Boolean);
                serverPayload.command = command;
                serverPayload.args = args;
            } else {
                const url = document.getElementById('mcpUrl')?.value.trim();
                if (!url) {
                    alert('请输入服务 URL');
                    return;
                }
                serverPayload.url = url;
            }

            try {
                const res = await this.fetchApi('/api/tools/servers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, server: serverPayload })
                });
                const data = await res.json();
                if (res.ok && data.code === 200) {
                    document.getElementById('mcpAddForm')?.reset();
                    this.switchTab('servers');
                    await this.loadData();
                } else {
                    alert('添加服务失败: ' + (data.detail || data.message || '未知错误'));
                }
            } catch (err) {
                alert('网络请求异常: ' + err.message);
            }
        },

        async handleDeleteServer(serverName) {
            if (!confirm(`确定要从自定义配置中移除 MCP 服务 "${serverName}" 吗？`)) return;
            try {
                const res = await this.fetchApi(`/api/tools/servers/${encodeURIComponent(serverName)}`, {
                    method: 'DELETE'
                });
                const data = await res.json();
                if (res.ok && data.code === 200) {
                    await this.loadData();
                } else {
                    alert('移除服务失败: ' + (data.detail || data.message || '未知错误'));
                }
            } catch (err) {
                alert('网络请求异常: ' + err.message);
            }
        },

        async handleSaveJson() {
            const editor = document.getElementById('mcpJsonEditor');
            const statusEl = document.getElementById('mcpJsonStatus');
            if (!editor) return;

            const content = editor.value.trim();
            try {
                JSON.parse(content);
            } catch (e) {
                if (statusEl) {
                    statusEl.innerHTML = `<p style="color: #ef4444;">❌ JSON 语法错误: ${escapeHtml(e.message)}</p>`;
                }
                return;
            }

            try {
                const res = await this.fetchApi('/api/tools/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content })
                });
                const data = await res.json();
                if (res.ok && data.code === 200) {
                    if (statusEl) {
                        statusEl.innerHTML = '<p style="color: #10b981;">✅ 配置已保存并已成功热重载！</p>';
                    }
                    await this.loadData();
                } else {
                    if (statusEl) {
                        statusEl.innerHTML = `<p style="color: #ef4444;">❌ 保存失败: ${escapeHtml(data.detail || data.message)}</p>`;
                    }
                }
            } catch (err) {
                if (statusEl) {
                    statusEl.innerHTML = `<p style="color: #ef4444;">❌ 网络异常: ${escapeHtml(err.message)}</p>`;
                }
            }
        },

        handleFormatJson() {
            const editor = document.getElementById('mcpJsonEditor');
            if (!editor) return;
            try {
                const parsed = JSON.parse(editor.value);
                editor.value = JSON.stringify(parsed, null, 2);
            } catch (e) {
                alert('JSON 语法错误，无法格式化: ' + e.message);
            }
        },

        handleResetJson() {
            const editor = document.getElementById('mcpJsonEditor');
            if (editor) {
                editor.value = this.rawJson;
            }
        },

        async handleRefreshAll() {
            const btn = document.getElementById('mcpRefreshAllBtn');
            const originalHtml = btn ? btn.innerHTML : '🔄 立即刷新探测';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = '<span class="mcp-spinner-sm"></span> 正在重新探测所有探针...';
            }
            try {
                await this.fetchApi('/api/tools/refresh', { method: 'POST' });
                // 延迟等待后台异步建立会话并完成发现
                setTimeout(async () => {
                    await this.loadData();
                    if (btn) {
                        btn.innerHTML = '✅ 探测完成';
                        setTimeout(() => {
                            btn.disabled = false;
                            btn.innerHTML = originalHtml;
                        }, 1200);
                    }
                }, 1500);
            } catch (err) {
                console.error('刷新失败:', err);
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '❌ 探测失败，点击重试';
                    setTimeout(() => { btn.innerHTML = originalHtml; }, 2000);
                }
            }
        }
    };

    window.McpManager = McpManager;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => McpManager.init());
    } else {
        McpManager.init();
    }
})();
