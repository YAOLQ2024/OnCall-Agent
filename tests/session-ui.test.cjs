// Exercise the shipped browser methods with controlled DOM/fetch; no external packages.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function appFixture() {
    const elements = new Map();
    const context = vm.createContext({
        console: {log() {}, error() {}, warn() {}}, TextDecoder, Headers, AbortController, setTimeout, clearTimeout,
        localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
        crypto: require('node:crypto').webcrypto,
        document: {
            createElement: () => ({}), head: {appendChild() {}}, addEventListener() {},
            getElementById: id => elements.get(id),
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8') +
        '\nglobalThis.App = OnCallAegntApp;', context);
    const app = Object.create(context.App.prototype);
    Object.assign(app, {
        apiBaseUrl: '/api', sessionId: 'original', isStreaming: false,
        accessToken: 'test-token', currentUser: {user_id: 'sre_alice'}, authVersion: 0,
        authBusy: false, pendingAuthRequests: new Set(),
        currentChatHistory: [], chatHistories: [], chatMessages: {innerHTML: ''},
        checkAndSetCentered() {}, renderChatHistory() {}, notifications: [],
        showNotification(message) {this.notifications.push(message);},
        addMessage() {},
    });
    return {app, context};
}

test('empty persisted session loads without referencing a missing local cache', async () => {
    const {app, context} = appFixture();
    context.fetch = async () => ({ok: true, json: async () => ({data: {messages: []}})});
    await app.loadChatHistory('empty');
    assert.equal(app.sessionId, 'empty');
    assert.equal(app.currentChatHistory.length, 0);
    assert.equal(app.notifications.length, 0);
});

test('history restores roles and full text from the server', async () => {
    const {app, context} = appFixture();
    const trace = [{type: 'route', data: {intent: 'aiops_diagnosis'}},
                   {type: 'aiops_progress', data: {type: 'plan', plan: ['Check metrics']}}];
    const messages = [{role: 'user', content: 'question'}, {role: 'assistant', content: 'report', execution_trace: trace}];
    context.fetch = async () => ({ok: true, json: async () => ({data: {messages}})});
    const rendered = [];
    app.addMessage = (type, content) => {
        rendered.push([type, content]);
        return content;
    };
    const rows = [];
    const accordion = {};
    app.createThoughtContainer = element => {
        assert.equal(element, 'report');
        return accordion;
    };
    app.addThoughtStep = (container, badge, label) => rows.push(label);
    app.updateThoughtSummary = () => {};
    app.escapeHtml = text => text;
    app.completeThoughtAccordion = (container, count) => {
        assert.equal(container, accordion);
        assert.equal(count, 2);
    };
    await app.loadChatHistory('saved');
    assert.deepEqual(rendered, [['user', 'question'], ['assistant', 'report']]);
    assert.deepEqual(rows, ['🎯 任务路由', '执行计划']);
    assert.equal(app.currentChatHistory.length, 2);
});

test('history errors retain the active conversation', async () => {
    const {app, context} = appFixture();
    context.fetch = async () => ({ok: false});
    await app.loadChatHistory('missing');
    assert.equal(app.sessionId, 'original');
    assert.equal(app.notifications.length, 1);
});

test('busy conversation blocks identity changes and history/delete requests', async () => {
    const {app, context} = appFixture();
    app.isStreaming = true;
    let calls = 0;
    context.fetch = async () => {calls++; throw Error('unexpected request');};
    app.loadCurrentUser = async () => {};
    await app.logout();
    await app.loadChatHistory('other');
    await app.deleteChatHistory('original');
    assert.equal(calls, 0);
    assert.equal(app.notifications.length, 1);
});

test('identity switch clears old identity cache before starting a new conversation', async () => {
    const {app, context} = appFixture();
    app.currentChatHistory = [{content: 'private'}];
    app.chatHistories = [{id: 'private'}];
    context.fetch = async () => ({ok: true, json: async () => ({code: 200, data: {
        access_token: 'bob-token', user: {user_id: 'dev_bob', display_name: 'Bob'}
    }})});
    app.loadToolStatus = async () => {};
    app.loadChatHistories = async () => {};
    app.newChat = () => {
        assert.equal(app.currentChatHistory.length, 0);
        assert.equal(app.chatHistories.length, 0);
    };
    await app.handleLoginSubmit('bob', '123456');
});

test('AIOps SSE keeps plans and step progress in the accordion and displays the final report', async () => {
    const {app, context} = appFixture();
    const sse = [{type: 'aiops_progress', data: {type: 'plan', plan: ['Read logs', 'Check metrics']}},
                 {type: 'aiops_progress', data: {type: 'step_complete', current_step: 'Read logs', remaining_steps: 1}},
                 {type: 'done', data: {answer: 'final persisted report'}}];
    context.fetch = async () => new Response(sse.map(e => 'data: ' + JSON.stringify(e) + '\r\n\r\n').join(''),
                                              {headers: {'Content-Type': 'text/event-stream'}});
    const body = {innerHTML: ''};
    const rows = [];
    const accordion = {hasAttribute: () => false};
    app.addMessage = () => ({querySelector: () => body});
    app.createThoughtContainer = () => accordion;
    app.updateThoughtSummary = () => {};
    app.escapeHtml = text => text;
    app.addThoughtStep = (container, badgeClass, label, text) => {
        assert.equal(container, accordion);
        assert.equal(body.innerHTML, '');
        rows.push([label, text]);
    };
    let completedRows;
    app.completeThoughtAccordion = (container, count) => {completedRows = count;};
    app.renderMarkdown = text => text;
    app.highlightCodeBlocks = () => {};
    app.scrollToBottom = () => {};
    let answer;
    app.handleStreamComplete = (element, text) => {answer = text;};
    await app.sendStreamMessage('aiops diagnose');
    assert.match(rows[0][1], /<details[^>]*>.*<ol><li>Read logs<\/li><li>Check metrics<\/li><\/ol>/);
    assert.deepEqual(rows[1], ['步骤结束', 'Read logs']);
    assert.equal(completedRows, 2);
    assert.equal(answer, 'final persisted report');
});

test('session ID generation also works on plain HTTP origins without randomUUID', () => {
    const {app, context} = appFixture();
    context.crypto = {getRandomValues: buffer => require('node:crypto').webcrypto.getRandomValues(buffer)};
    const first = app.generateSessionId();
    assert.match(first, /^session_/);
    assert.notEqual(first, app.generateSessionId());
});

test('401 clears private data and aborts all old authenticated requests', async () => {
    const {app, context} = appFixture();
    app.currentChatHistory = [{content: 'private'}];
    app.chatHistories = [{id: 'private'}];
    app.chatMessages.innerHTML = 'private';
    app.loginPassword = {value: 'password'};
    let shown = false;
    app.showLoginOverlay = () => {shown = true;};
    const pending = new AbortController();
    app.pendingAuthRequests.add(pending);
    context.fetch = async () => new Response('{}', {status: 401});
    const response = await app.authFetch('/api/sessions');
    assert.equal(response.status, 401);
    assert.equal(app.accessToken, '');
    assert.equal(app.currentUser, null);
    assert.equal(app.currentChatHistory.length, 0);
    assert.equal(app.chatHistories.length, 0);
    assert.equal(app.chatMessages.innerHTML, '');
    assert.equal(app.loginPassword.value, '');
    assert.equal(pending.signal.aborted, true);
    assert.equal(shown, true);
});

test('old 401 cannot sign out a newly logged-in identity', async () => {
    const {app, context} = appFixture();
    let complete;
    context.fetch = () => new Promise(resolve => {complete = resolve;});
    const pending = app.authFetch('/api/sessions');
    app.clearAuthState();
    app.accessToken = 'new-bob-token';
    app.currentUser = {user_id: 'dev_bob'};
    complete(new Response('{}', {status: 401}));
    await assert.rejects(pending, /登录身份已变化/);
    assert.equal(app.accessToken, 'new-bob-token');
    assert.equal(app.currentUser.user_id, 'dev_bob');
});

test('old history body cannot overwrite a new identity after JSON parsing', async () => {
    const {app, context} = appFixture();
    let complete;
    context.fetch = async () => ({status: 200, ok: true,
        json: () => new Promise(resolve => {complete = resolve;})});
    const response = await app.authFetch('/api/sessions');
    const json = response.json();
    app.clearAuthState();
    app.accessToken = 'bob';
    complete({data: {sessions: [{title: 'Alice private'}]}});
    await assert.rejects(json, /登录身份已变化/);
    assert.equal(app.chatHistories.length, 0);
});

test('cookie-only login is restored when localStorage has no token', async () => {
    const {app, context} = appFixture();
    app.accessToken = '';
    app.currentUser = null;
    context.fetch = async () => new Response(JSON.stringify({data: {user_id: 'sre_alice', display_name: 'Alice'}}));
    let hidden = false, loaded = false;
    app.hideLoginOverlay = () => {hidden = true;};
    app.loadChatHistories = async () => {loaded = true;};
    app.loadToolStatus = async () => {};
    await app.initAuth();
    assert.equal(app.currentUser.user_id, 'sre_alice');
    assert.equal(hidden, true);
    assert.equal(loaded, true);
});

test('double-click login only sends one authentication request', async () => {
    const {app, context} = appFixture();
    let complete, calls = 0;
    context.fetch = () => {calls++; return new Promise(resolve => {complete = resolve;});};
    app.loadChatHistories = async () => {};
    app.loadToolStatus = async () => {};
    app.newChat = () => {};
    const first = app.handleLoginSubmit('alice', '123456');
    await app.handleLoginSubmit('bob', '123456');
    assert.equal(calls, 1);
    complete(new Response(JSON.stringify({code: 200, data: {
        access_token: 'alice-token', user: {user_id: 'sre_alice', display_name: 'Alice'}
    }})));
    await first;
    assert.equal(app.currentUser.user_id, 'sre_alice');
    assert.equal(app.authBusy, false);
});

test('failed logout retains login so an uncleared HttpOnly cookie is not hidden', async () => {
    const {app, context} = appFixture();
    context.fetch = async () => new Response('{}', {status: 500});
    await app.logout();
    assert.equal(app.accessToken, 'test-token');
    assert.equal(app.currentUser.user_id, 'sre_alice');
    assert.equal(app.notifications.length, 1);
    assert.equal(app.authBusy, false);
});

test('late initAuth failure cannot reopen the login overlay for a new identity', async () => {
    const {app, context} = appFixture();
    let reject;
    context.fetch = () => new Promise((resolve, fail) => {reject = fail;});
    let shown = false;
    app.showLoginOverlay = () => {shown = true;};
    const pending = app.initAuth();
    app.clearAuthState();
    app.currentUser = {user_id: 'dev_bob'};
    app.accessToken = 'bob-token';
    reject(new Error('old request failed'));
    await pending;
    assert.equal(shown, false);
    assert.equal(app.currentUser.user_id, 'dev_bob');
});

test('authFetch preserves Headers instances and adds the Bearer token', async () => {
    const {app, context} = appFixture();
    let received;
    context.fetch = async (url, options) => {received = options.headers; return new Response('{}');};
    await app.authFetch('/api/sessions', {headers: new Headers({'X-Test': 'preserved'})});
    assert.equal(received.get('X-Test'), 'preserved');
    assert.equal(received.get('Authorization'), 'Bearer test-token');
});
