/* Incident management within the existing authenticated memory dialog. */
class IncidentMemoryView {
    static labels = {draft: '待确认', confirmed: '已确认', invalidated: '已失效', withdrawn: '已撤回'};
    static outcomes = {resolved: '已恢复', unresolved: '未解决'};
    static path = '/api/memory/incidents';

    constructor(app) {
        this.app = app;
        const ids = [
            'ProfileTab', 'ProfilePane', 'IncidentTab', 'IncidentPane', 'JobsTab', 'JobsPane', 'IncidentBrowse', 'IncidentEditor',
            'IncidentNew', 'IncidentAll', 'IncidentFilter', 'IncidentSearch', 'IncidentQuery',
            'IncidentList', 'IncidentMore', 'IncidentBack', 'IncidentReload', 'IncidentHeading',
            'IncidentMeta', 'IncidentForm', 'IncidentFields', 'IncidentMessages',
            'IncidentMessageList', 'IncidentActions', 'IncidentConfirmation', 'IncidentUnresolved',
            'IncidentResolved', 'IncidentInvalidation', 'IncidentInvalidateForm', 'IncidentInvalidate',
            'IncidentWithdrawal', 'IncidentWithdraw', 'IncidentStatus',
        ];
        this.ui = Object.fromEntries(ids.map(id => [id, document.getElementById('memory' + id)]));
        this.form = this.ui.IncidentForm;
        this.inputs = [...this.form.elements].filter(input => input.name);
        this.source = this.form.elements.source_session_id;
        this.invalidateReason = document.getElementById('incidentInvalidateReason');
        this.bind();
        this.reset();
    }

    bind() {
        const on = (key, event, handler) => this.ui[key].addEventListener(event, handler);
        on('ProfileTab', 'click', () => this.setPane('Profile'));
        on('IncidentTab', 'click', () => {
            this.setPane('Incident');
            if (!this.loaded && !this.controller && this.ui.IncidentEditor.hidden) this.loadList();
        });
        on('JobsTab', 'click', () => {
            this.setPane('Jobs');
            this.app.memoryJobs?.show();
        });
        on('IncidentNew', 'click', () => this.openEditor(null));
        on('IncidentAll', 'click', () => this.reloadList());
        on('IncidentFilter', 'change', () => this.reloadList());
        on('IncidentMore', 'click', () => this.loadList(this.cursor));
        on('IncidentSearch', 'submit', event => {
            event.preventDefault();
            const query = this.ui.IncidentQuery.value.trim();
            if (query) this.loadList(null, query);
        });
        on('IncidentBack', 'click', () => this.back());
        on('IncidentReload', 'click', () => this.loadRecord(this.record.memory_id));
        on('IncidentForm', 'submit', event => { event.preventDefault(); this.save(); });
        on('IncidentForm', 'input', () => this.updateControls());
        this.source.addEventListener('change', () => {
            this.form.elements.source_message_ids.value = '';
            this.form.elements.recovery_ids.value = '';
            this.clearMessages();
            this.updateControls();
        });
        on('IncidentMessages', 'click', () => this.loadMessages());
        on('IncidentUnresolved', 'click', () => this.confirm('unresolved'));
        on('IncidentResolved', 'click', () => this.confirm('resolved'));
        on('IncidentInvalidateForm', 'submit', event => {
            event.preventDefault();
            const reason = this.invalidateReason.value.trim();
            if (reason) this.action('/invalidate', 'POST', {reason}, '事故已失效，已移出词法索引。');
        });
        on('IncidentWithdraw', 'click', () => this.action('', 'DELETE', null, '事故已撤回，正文和索引已清除。'));
    }

    reset() {
        this.controller?.abort();
        this.controller = null;
        this.loaded = false;
        this.cursor = null;
        this.record = null;
        this.original = '';
        this.conflict = false;
        this.ui.IncidentFilter.value = '';
        this.ui.IncidentQuery.value = '';
        this.ui.IncidentList.replaceChildren();
        this.ui.IncidentMeta.textContent = '';
        this.form.reset();
        this.form.querySelectorAll('details').forEach(details => { details.open = false; });
        this.source.replaceChildren(new Option('无关联会话', ''));
        this.invalidateReason.value = '';
        this.clearMessages();
        this.setPane('Profile');
        this.ui.IncidentBrowse.hidden = false;
        this.ui.IncidentEditor.hidden = true;
        this.status('', '尚未加载事故经验。');
        this.updateControls();
    }

    setPane(active) {
        for (const name of ['Profile', 'Incident', 'Jobs']) {
            this.ui[name + 'Pane'].hidden = name !== active;
            this.ui[name + 'Tab'].setAttribute('aria-pressed', String(name === active));
        }
        if (active !== 'Jobs') this.app.memoryJobs?.hide();
    }

    status(state, message, reveal = false) {
        this.ui.IncidentStatus.dataset.state = state;
        this.ui.IncidentStatus.firstElementChild.textContent = message;
        if (reveal && !this.ui.IncidentPane.hidden) this.ui.IncidentStatus.scrollIntoView({block: 'nearest'});
    }

    async request(path, options, apply, {mutation = false} = {}) {
        if (this.controller || !this.app.currentUser || !this.app.memoryDialog.open) return;
        const controller = new AbortController();
        const authVersion = this.app.authVersion;
        const current = () => this.controller === controller && this.app.authVersion === authVersion
            && this.app.memoryDialog.open;
        this.controller = controller;
        this.updateControls();
        this.status('running', '正在处理…');
        try {
            const response = await this.app.authFetch(path, {...options, signal: controller.signal});
            if (!current()) return;
            const result = await response.json().catch(() => ({}));
            if (!current()) return;
            if (response.status === 409 && mutation) {
                this.conflict = true;
                this.status('warning', '记录状态或版本已变更，输入仍保留。请核对后点击重新加载记录。', true);
            } else if (!response.ok || !result.data) {
                const detail = result.detail;
                const message = typeof detail === 'string' ? detail
                    : Array.isArray(detail) ? this.validationMessage(detail[0])
                    : '请求失败，请重试。';
                this.status('error', message, true);
            } else {
                apply(result.data);
            }
        } catch (error) {
            if (current()) this.status('error', error.message || '请求失败，请重试。', true);
        } finally {
            if (current()) {
                this.controller = null;
                this.updateControls();
            }
        }
    }

    validationMessage(issue) {
        const field = issue?.loc?.find(name => this.form.elements.namedItem(String(name))?.labels?.length);
        const label = field && this.form.elements.namedItem(field).labels[0].textContent;
        return label ? `请检查「${label}」的格式、长度或数量。` : '请检查输入格式、长度和必填项。';
    }

    reloadList() {
        this.ui.IncidentQuery.value = '';
        return this.loadList();
    }

    loadList(cursor = null, query = '') {
        const params = new URLSearchParams();
        if (query) params.set('q', query);
        else if (this.ui.IncidentFilter.value) params.set('status', this.ui.IncidentFilter.value);
        if (cursor) params.set('cursor', cursor);
        const suffix = query ? '/search' : '';
        return this.request(`${IncidentMemoryView.path}${suffix}?${params}`, {}, data => {
            this.loaded = true;
            this.cursor = data.next_cursor || null;
            if (!cursor) this.ui.IncidentList.replaceChildren();
            for (const item of data.items) {
                const li = document.createElement('li');
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'memory-profile-secondary';
                button.textContent = [item.title || '已撤回事故', IncidentMemoryView.labels[item.status],
                    IncidentMemoryView.outcomes[item.outcome], item.environment].filter(Boolean).join(' · ');
                button.addEventListener('click', () => this.loadRecord(item.memory_id));
                li.append(button);
                this.ui.IncidentList.append(li);
            }
            const degraded = data.lexical_mode === 'degraded';
            const message = data.items.length ? (query ? '已搜索已确认事故。' : '事故列表已加载。') : '暂无符合条件的事故。';
            this.status(degraded ? 'warning' : 'success', message + (degraded ? ' 当前仅支持精确服务名、环境和错误码匹配。' : ''));
        });
    }

    loadRecord(id) {
        return this.request(`${IncidentMemoryView.path}/${encodeURIComponent(id)}`, {}, data => this.openEditor(data));
    }

    openEditor(record, message = '') {
        this.record = record;
        this.conflict = false;
        this.ui.IncidentBrowse.hidden = true;
        this.ui.IncidentEditor.hidden = false;
        this.form.reset();
        this.form.querySelectorAll('details').forEach(details => { details.open = false; });
        this.ui.IncidentConfirmation.open = this.ui.IncidentInvalidation.open = this.ui.IncidentWithdrawal.open = false;
        this.invalidateReason.value = '';
        this.clearMessages();
        const incident = record?.incident || {};
        this.source.replaceChildren(new Option('无关联会话', ''));
        const sessions = new Map(this.app.chatHistories.map(item => [item.id, item.title]));
        if (incident.source_session_id && !sessions.has(incident.source_session_id)) {
            sessions.set(incident.source_session_id, '原关联会话');
        }
        for (const [id, title] of sessions) this.source.add(new Option(title, id));
        const evidence = incident.recovery_evidence;
        const values = {...incident, recovery_summary: evidence?.summary, recovery_url: evidence?.reference_url,
            recovery_ids: evidence?.message_ids};
        for (const input of this.inputs) {
            const value = values[input.name];
            input.value = Array.isArray(value) ? value.join(', ')
                : input.type === 'datetime-local' && value ? this.localDate(value) : (value ?? '');
        }
        this.original = this.inputState();
        const withdrawn = record?.status === 'withdrawn';
        this.form.hidden = withdrawn;
        this.ui.IncidentHeading.textContent = withdrawn ? '已撤回事故' : record ? '事故详情' : '新建事故草稿';
        this.ui.IncidentMeta.textContent = record
            ? [IncidentMemoryView.labels[record.status], IncidentMemoryView.outcomes[record.outcome], `版本 ${record.revision}`,
                record.confirmed_at && `确认于 ${new Date(record.confirmed_at).toLocaleString()}`,
                record.invalidation_reason && `失效原因：${record.invalidation_reason}`].filter(Boolean).join(' · ')
            : '草稿可以先保存，确认时须填写症状、目标服务、适用环境和发生时间。';
        this.status('success', message || (withdrawn ? '该事故的正文和证据已清除。' : '请核对事故事实和适用条件。'), Boolean(message));
        this.updateControls();
    }

    localDate(value) {
        const date = new Date(value);
        return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 23);
    }

    back() {
        this.openEditor(null);
        this.ui.IncidentEditor.hidden = true;
        this.ui.IncidentBrowse.hidden = false;
        this.reloadList();
    }

    inputState() {
        return JSON.stringify(this.inputs.map(input => input.value));
    }

    updateControls() {
        const busy = Boolean(this.controller);
        const status = this.record?.status;
        const dirty = this.original !== this.inputState();
        const blocked = busy || this.conflict;
        for (const key of ['IncidentNew', 'IncidentAll', 'IncidentFilter', 'IncidentQuery', 'IncidentBack']) this.ui[key].disabled = busy;
        this.ui.IncidentSearch.querySelector('button').disabled = busy;
        this.ui.IncidentList.inert = busy;
        this.ui.IncidentMore.hidden = !this.cursor;
        this.ui.IncidentMore.disabled = busy;
        this.ui.IncidentFields.disabled = busy;
        this.ui.IncidentForm.querySelector('button[type="submit"]').disabled = blocked;
        this.ui.IncidentReload.hidden = !this.record;
        this.ui.IncidentReload.disabled = busy;
        this.ui.IncidentMessages.disabled = busy || !this.source.value;
        this.ui.IncidentActions.hidden = !status || status === 'withdrawn';
        this.ui.IncidentConfirmation.hidden = status !== 'draft';
        this.ui.IncidentResolved.disabled = this.ui.IncidentUnresolved.disabled = blocked || dirty;
        this.ui.IncidentInvalidation.hidden = status !== 'confirmed';
        this.ui.IncidentInvalidate.disabled = this.ui.IncidentWithdraw.disabled = blocked;
        const save = status === 'confirmed' || status === 'invalidated' ? '保存并重新评审' : '保存草稿';
        this.ui.IncidentForm.querySelector('button[type="submit"]').textContent = save;
    }

    clearMessages() {
        this.ui.IncidentMessageList.replaceChildren();
        this.ui.IncidentMessageList.hidden = true;
    }

    loadMessages() {
        if (!this.source.value) return;
        this.request(`/api/sessions/${encodeURIComponent(this.source.value)}/message-references`, {}, data => {
            this.clearMessages();
            for (const message of data.messages) {
                const preview = document.createElement('p');
                preview.textContent = `#${message.id} · ${message.role === 'user' ? '用户' : '助手'} · ${message.preview}`;
                this.ui.IncidentMessageList.append(preview);
            }
            this.ui.IncidentMessageList.hidden = false;
            this.status('success', data.messages.length ? '可将上方消息编号填写到来源消息中。' : '该会话尚无已保存的消息。');
        });
    }

    formChanges() {
        const original = JSON.parse(this.original);
        const fields = this.record?.incident || {};
        const changes = {};
        let evidenceChanged = false;
        const split = value => value.split(/[,，\n]+/).map(item => item.trim()).filter(Boolean);
        const ids = value => split(value).map(id => {
            const numeric = Number(id);
            if (!/^\d+$/.test(id) || !Number.isSafeInteger(numeric) || numeric < 1) {
                throw new Error('消息编号须为正整数，用逗号分隔。');
            }
            return numeric;
        });
        for (const [index, input] of this.inputs.entries()) {
            const field = input.name;
            const value = input.value.trim();
            if (input.value === original[index]) continue;
            if (field.startsWith('recovery_')) {
                evidenceChanged = true;
                continue;
            }
            if (!value) changes[field] = null;
            else if (field === 'source_message_ids') changes[field] = ids(value);
            else if (field === 'services' || field === 'error_codes') changes[field] = split(value);
            else if (input.type === 'datetime-local') changes[field] = new Date(value).toISOString();
            else changes[field] = value;
        }
        if (evidenceChanged) {
            const summary = this.form.elements.recovery_summary.value.trim();
            const reference_url = this.form.elements.recovery_url.value.trim() || null;
            const message_ids = ids(this.form.elements.recovery_ids.value);
            if ((summary || reference_url || message_ids.length) && (!summary || (!reference_url && !message_ids.length))) {
                throw new Error('恢复证据须包含摘要，以及监控链接或来源消息编号。');
            }
            changes.recovery_evidence = summary ? {summary, reference_url, message_ids} : null;
        }
        // Keep unchanged values out of the patch, including timestamps with greater precision than HTML inputs.
        return Object.fromEntries(Object.entries(changes).filter(([key, value]) => value !== (fields[key] ?? null)));
    }

    json(method, data) {
        return {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)};
    }

    save() {
        if (this.conflict) return;
        try {
            const changes = this.formChanges();
            const record = this.record;
            if (!record && !changes.title) throw new Error('请输入事故标题。');
            const path = record ? `${IncidentMemoryView.path}/${record.memory_id}` : IncidentMemoryView.path;
            const body = record ? {expected_revision: record.revision, changes} : changes;
            return this.request(path, this.json(record ? 'PATCH' : 'POST', body),
                data => this.openEditor(data, data.status === 'draft' ? '草稿已保存。' : '记录未发生变化。'), {mutation: true});
        } catch (error) {
            this.status('error', error.message, true);
        }
    }

    confirm(outcome) {
        if (this.inputState() !== this.original) return;
        this.action('/confirm', 'POST', {outcome}, '事故已确认，词法索引已更新。');
    }

    action(suffix, method, data, message) {
        if (!this.record || this.conflict) return;
        const revision = this.record.revision;
        const path = `${IncidentMemoryView.path}/${this.record.memory_id}${suffix}`;
        const apply = result => {
            const feedback = result.status === 'confirmed' && result.lexical_mode === 'degraded'
                ? '事故已确认，当前仅支持精确标识符匹配。' : message;
            this.openEditor(result, feedback);
        };
        if (method === 'DELETE') {
            return this.request(`${path}?expected_revision=${revision}`, {method}, apply, {mutation: true});
        }
        return this.request(path, this.json(method, {expected_revision: revision, ...data}), apply, {mutation: true});
    }
}
