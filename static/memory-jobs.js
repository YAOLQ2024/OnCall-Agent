/* Background memory jobs inside the existing long-term memory dialog. */
class MemoryJobsView {
    static titles = {
        extract_profile_proposal: '画像提议', extract_incident_draft: '事故提议',
        index_incident: '同步向量', purge_incident_index: '清理向量',
    };
    static states = {pending: '待处理', running: '处理中', succeeded: '已完成',
        skipped: '已跳过', failed: '失败', cancelled: '已取消'};
    static errors = {budget_exhausted: '额度不足', invalid_proposal: '结果或引用未通过校验',
        temporary_failure: '服务暂时不可用', provider_rejected: '外部服务拒绝请求', lease_expired: '任务超时'};
    static fields = {preferred_language: '语言', answer_detail: '详细度', preferred_output_order: '输出顺序',
        default_cluster: '集群', default_environment: '环境', default_namespace: '命名空间',
        responsible_services: '负责服务', response_preferences: '自然语言回答偏好', title: '标题', symptoms: '症状', services: '目标服务',
        cluster: '集群', environment: '环境', namespace: '命名空间', occurred_at: '发生时间',
        version_constraints: '适用版本', diagnostic_actions: '诊断动作', excluded_hypotheses: '已排除假设',
        root_cause: '根因', actions_taken: '采取的操作', lessons: '复盘结论', error_codes: '错误码',
        source_session_id: '来源会话', source_message_ids: '引用消息'};

    constructor(app) {
        this.app = app;
        const ids = ['JobsPane', 'JobSession', 'JobSubmit', 'JobUsage', 'JobRefresh', 'JobMore', 'JobList',
            'JobDetail', 'JobHeading', 'JobProposal', 'JobApply', 'JobDismiss', 'JobRetry', 'JobStatus',
            'ForgetSection', 'ForgetForm', 'ForgetCheck', 'ForgetSubmit'];
        this.ui = Object.fromEntries(ids.map(id => [id, document.getElementById('memory' + id)]));
        this.ui.JobRefresh.addEventListener('click', () => this.list());
        this.ui.JobMore.addEventListener('click', () => this.list(this.cursor));
        this.ui.JobSession.addEventListener('change', () => {
            this.requestId = null;
            this.controls();
        });
        this.ui.JobSubmit.addEventListener('click', () => this.submit());
        this.ui.JobApply.addEventListener('click', () => this.review('apply'));
        this.ui.JobDismiss.addEventListener('click', () => this.review('dismiss'));
        this.ui.JobRetry.addEventListener('click', () => this.retry());
        this.ui.ForgetForm.addEventListener('submit', event => {
            event.preventDefault();
            this.forget();
        });
        this.reset();
    }

    hide() {
        this.view = (this.view || 0) + 1;
        this.controller?.abort();
        this.controller = null;
        clearTimeout(this.timer);
    }

    reset() {
        this.hide();
        this.selected = null;
        this.cursor = null;
        this.requestId = null;
        this.available = false;
        this.ui.JobSession.replaceChildren(new Option('请选择', ''));
        this.ui.JobList.replaceChildren();
        this.ui.JobDetail.hidden = true;
        this.ui.JobHeading.textContent = '';
        this.ui.JobProposal.replaceChildren();
        this.ui.JobUsage.textContent = '';
        this.ui.ForgetCheck.checked = false;
        this.ui.ForgetSection.open = false;
        this.status('', '');
        this.controls();
    }

    async show(message = '') {
        this.reset();
        const view = this.view;
        await this.list();
        if (!this.active(view)) return;
        await this.loadSessions();
        if (message && this.active(view)) this.status('success', message);
    }

    active(view) {
        return view === this.view && !this.ui.JobsPane.hidden && this.app.currentUser && this.app.memoryDialog.open;
    }

    status(state, text) {
        this.ui.JobStatus.dataset.state = state;
        this.ui.JobStatus.firstElementChild.textContent = text;
    }

    controls() {
        const busy = Boolean(this.controller);
        this.ui.JobSubmit.disabled = busy || !this.available || !this.ui.JobSession.value;
        for (const key of ['JobSession', 'JobRefresh', 'JobMore', 'ForgetSubmit', 'JobApply', 'JobDismiss', 'JobRetry']) {
            this.ui[key].disabled = busy;
        }
        this.ui.JobMore.hidden = !this.cursor;
        this.ui.JobList.inert = busy;
    }

    async request(path, options = {}) {
        if (this.controller || this.ui.JobsPane.hidden || !this.app.currentUser || !this.app.memoryDialog.open) return null;
        const controller = new AbortController();
        const version = this.app.authVersion;
        const view = this.view;
        this.controller = controller;
        this.controls();
        const current = () => this.controller === controller && this.app.authVersion === version && this.active(view);
        try {
            const response = await this.app.authFetch(path, {...options, cache: 'no-store', signal: controller.signal});
            const result = await response.json();
            if (!current()) return null;
            if (!response.ok || !result.data) {
                this.status('error', typeof result.detail === 'string' ? result.detail : '请求未完成，请重试。');
                return null;
            }
            return result.data;
        } catch (error) {
            if (current()) this.status('error', error.message || '请求未完成，请重试。');
            return null;
        } finally {
            if (current()) {
                this.controller = null;
                this.controls();
            }
        }
    }

    post(path, body) {
        return this.request(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    }

    async list(cursor) {
        const data = await this.request('/api/memory/jobs' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
        if (!data) return;
        this.available = data.manual_extraction_available;
        this.cursor = data.next_cursor;
        if (!cursor) this.ui.JobList.replaceChildren();
        for (const job of data.jobs) {
            const item = document.createElement('li');
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'memory-profile-secondary';
            button.dataset.jobId = job.job_id;
            button.textContent = this.labelWithDate(job);
            button.addEventListener('click', () => this.select(job.job_id));
            item.append(button);
            this.ui.JobList.append(item);
        }
        const used = data.usage;
        this.ui.JobUsage.textContent = `本账号今日用量 · 输入 ${used.input_tokens} / 输出 ${used.output_tokens} token · 向量 ${used.embedding_items} 条`;
        this.ui.JobUsage.title = `应用共享日额度 · 输入 ${data.limits.input_tokens} / 输出 ${data.limits.output_tokens} token · 向量 ${data.limits.embedding_items} 条`;
        this.status('', this.available ? '' : '服务端提取开关或日额度尚未配置。');
        this.controls();
    }

    async loadSessions() {
        const data = await this.request('/api/sessions');
        if (!data) return;
        for (const session of data.sessions) {
            this.ui.JobSession.add(new Option(session.title, session.session_id));
        }
    }

    async submit() {
        const view = this.view;
        if (!this.requestId) this.requestId = this.app.generateSessionId();
        const job = await this.post('/api/memory/incidents/from-session',
            {session_id: this.ui.JobSession.value, request_id: this.requestId});
        if (!job) return;
        this.requestId = null;
        await this.list();
        if (!this.active(view)) return;
        await this.select(job.job_id);
        if (this.active(view)) this.status('success', '提取任务已受理，可在这里查看进度。');
    }

    label(job) {
        const state = job.requires_confirmation ? '待审核' : MemoryJobsView.states[job.status] || job.status;
        return `${MemoryJobsView.titles[job.kind] || job.kind} · ${state}`;
    }

    labelWithDate(job) {
        return `${this.label(job)} · ${new Date(job.created_at).toLocaleString()}`;
    }

    async select(id) {
        clearTimeout(this.timer);
        if (this.controller && this.selected?.job_id === id) {
            this.timer = setTimeout(() => this.select(id), 1500);
            return;
        }
        const job = await this.request('/api/memory/jobs/' + encodeURIComponent(id));
        if (!job) return;
        this.selected = job;
        this.ui.JobDetail.hidden = false;
        this.ui.JobHeading.textContent = this.label(job);
        for (const button of this.ui.JobList.querySelectorAll('button')) {
            if (button.dataset.jobId === id) button.textContent = this.labelWithDate(job);
        }
        this.ui.JobProposal.replaceChildren();
        this.ui.JobApply.hidden = this.ui.JobDismiss.hidden = !job.requires_confirmation;
        this.ui.JobRetry.hidden = !job.can_retry;
        this.ui.JobApply.textContent = job.kind === 'extract_profile_proposal' ? '应用所选字段' : '保存为事故草稿';
        if (job.error_code) this.paragraph(MemoryJobsView.errors[job.error_code] || '处理失败');
        if (job.retry_scheduled) this.paragraph('已安排自动重试。');
        if (job.proposal?.changes) {
            for (const [key, value] of Object.entries(job.proposal.changes)) {
                const label = document.createElement('label');
                label.className = 'memory-profile-toggle';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.value = key;
                input.checked = true;
                label.append(input, `${MemoryJobsView.fields[key] || key} · ${this.format(job.proposal.before[key])} → ${this.format(value)}`);
                this.ui.JobProposal.append(label);
            }
        }
        for (const [key, value] of Object.entries(job.proposal?.draft || {})) {
            this.paragraph(`${MemoryJobsView.fields[key] || key} · ${this.format(value)}`);
        }
        for (const evidence of job.proposal?.evidence || []) {
            const field = evidence.field ? MemoryJobsView.fields[evidence.field] + ' · ' : '';
            this.paragraph(`${field}消息 #${evidence.message_id} · ${evidence.quote}`);
        }
        if (['pending', 'running'].includes(job.status)) this.timer = setTimeout(() => this.select(id), 1500);
    }

    format(value) {
        return Array.isArray(value) ? value.join('、') : (value ?? '空');
    }

    paragraph(text) {
        const p = document.createElement('p');
        p.textContent = text;
        this.ui.JobProposal.append(p);
    }

    async review(action) {
        const view = this.view;
        const job = this.selected;
        if (!job?.requires_confirmation) return;
        const payload = {action};
        if (action === 'apply' && job.kind === 'extract_profile_proposal') {
            payload.fields = [...this.ui.JobProposal.querySelectorAll('input:checked')].map(input => input.value);
            if (!payload.fields.length) {
                this.status('warning', '请选择要应用的画像字段。');
                return;
            }
        }
        const result = await this.post('/api/memory/jobs/' + encodeURIComponent(job.job_id) + '/review', payload);
        if (!result) return;
        if (result.memory_id) this.app.incidentMemory.loaded = false;
        if (result.profile_revision !== undefined) {
            this.app.invalidateMemoryProfile();
            await this.app.loadMemoryProfile();
        }
        if (!this.active(view)) return;
        await this.list();
        if (!this.active(view)) return;
        await this.select(job.job_id);
        if (this.active(view)) this.status('success', action === 'dismiss' ? '已忽略提议。'
            : result.memory_id ? '已保存事故草稿。请到「事故经验」核实并确认。' : '已更新个人画像。');
    }

    async retry() {
        const job = this.selected;
        if (!job?.can_retry) return;
        const view = this.view;
        const result = await this.request('/api/memory/jobs/' + encodeURIComponent(job.job_id) + '/retry', {method: 'POST'});
        if (!result || !this.active(view)) return;
        await this.select(job.job_id);
    }

    async forget() {
        if (!this.ui.ForgetCheck.checked) return;
        const profile = this.app.memoryProfile;
        if (!profile) {
            this.status('error', '请先加载当前画像。');
            return;
        }
        const result = await this.post('/api/memory/forget', {expected_generation: profile.generation, confirm: true});
        if (!result) return;
        this.app.incidentMemory.reset();
        this.app.incidentMemory.setPane('Jobs');
        const view = this.view;
        this.app.invalidateMemoryProfile();
        await this.app.loadMemoryProfile();
        if (this.active(view)) await this.show(`已清空长期记忆，撤回 ${result.withdrawn_count} 条事故。`);
    }
}
