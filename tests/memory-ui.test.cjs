// Actual M1 browser methods with deterministic DOM/fetch; no third-party packages.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
    const context = vm.createContext({
        console, Headers, AbortController, TextDecoder, setTimeout, clearTimeout,
        crypto: require('node:crypto').webcrypto,
        localStorage: {removeItem() {}},
        document: {createElement: () => ({}), head: {appendChild() {}}, addEventListener() {}},
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8') + '\nglobalThis.App = OnCallAegntApp;', context);
    const app = Object.create(context.App.prototype);
    const keys = ['preferred_language','answer_detail','preferred_output_order','default_cluster','default_environment','default_namespace','responsible_services','response_preferences'];
    const inputs = Object.fromEntries(keys.map(key => [key, {value: ''}]));
    Object.assign(app, {
        currentUser: {user_id: 'sre_alice', display_name: 'Alice'}, accessToken: 'alice', authVersion: 0,
        pendingAuthRequests: new Set(), memoryProfile: null, memoryProfileRequest: 0,
        memoryProfileBusy: false, memoryProfileConflict: false,
        memoryDialog: {open: true, close() {this.open = false;}, showModal() {this.open = true;}},
        memoryProfileForm: {reset() {for (const input of Object.values(inputs)) input.value = '';}},
        memoryProfileInputs: inputs, memoryProfileFields: {}, memoryProfileOwner: {}, memoryUseEnabled: {},
        memorySaveBtn: {}, memoryReloadBtn: {}, memoryClearBtn: {},
        memoryProfileStatus: {dataset: {}}, memoryProfileStatusText: {},
        renderChatHistory() {}, checkAndSetCentered() {}, showLoginOverlay() {},
        generateSessionId: () => 'new-session',
    });
    return {app, context};
}
function data(profile = {}, revision = 0, use_enabled = false) {
    return {profile, revision, generation: 0, use_enabled, auto_extract_enabled: false, runtime_connected: false};
}
function response(value, status = 200) {return new Response(JSON.stringify(value), {status});}

test('empty profile loads and enables manual editing without claiming runtime integration', async () => {
    const {app, context} = fixture();
    context.fetch = async () => response({data: data()});
    await app.loadMemoryProfile();
    assert.equal(app.memoryProfile.revision, 0);
    assert.equal(app.memoryProfileFields.disabled, false);
    assert.equal(app.memorySaveBtn.disabled, false);
    assert.match(app.memoryProfileStatusText.textContent, /尚未用于生成回复/);
});

test('save sends only changed fields and expected revision, deduplicates services, clears nullable fields', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data({default_namespace: 'prod-pay', answer_detail: 'normal'}, 7));
    app.memoryProfileInputs.default_namespace.value = '';
    app.memoryProfileInputs.responsible_services.value = 'payment, redis，payment';
    app.memoryUseEnabled.checked = true;
    let payload;
    context.fetch = async (url, options) => {
        payload = JSON.parse(options.body);
        return response({data: data({answer_detail: 'normal', responsible_services: ['payment','redis']}, 8, true)});
    };
    await app.saveMemoryProfile();
    assert.deepEqual(payload, {expected_revision: 7, changes: {default_namespace: null, responsible_services: ['payment','redis']}, use_enabled: true});
    assert.equal(app.memoryProfile.revision, 8);
    assert.equal(app.memoryProfileBusy, false);
    assert.match(app.memoryProfileStatusText.textContent, /画像已保存/);
});

test('saving an empty untouched profile is a no-op patch', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data());
    let payload;
    context.fetch = async (url, options) => {payload = JSON.parse(options.body); return response({data: data()});};
    await app.saveMemoryProfile();
    assert.deepEqual(payload, {expected_revision: 0, changes: {}});
});

test('response preferences use one line per item and keep punctuation within an item', async () => {
    const {app, context} = fixture();
    const input = app.memoryProfileInputs.response_preferences;
    app.applyMemoryProfile(data({response_preferences: ['先给结论，随后举例', '术语附翻译']}, 1));
    assert.equal(input.value, '先给结论，随后举例\n术语附翻译');
    input.value = ' 只在首次出现时翻译 \r\n\r\n举例，说明适用条件\r\n只在首次出现时翻译';
    const preferences = ['只在首次出现时翻译', '举例，说明适用条件'];
    context.fetch = async (url, options) => {
        assert.deepEqual(JSON.parse(options.body), {expected_revision: 1, changes: {response_preferences: preferences}});
        return response({data: data({response_preferences: preferences}, 2)});
    };
    await app.saveMemoryProfile();
    assert.equal(input.value, preferences.join('\n'));
    assert.equal(app.memoryProfile.revision, 2);
});

test('409 preserves edits and blocks overwrite until explicit reload', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data({default_namespace:'prod-pay'}, 1));
    app.memoryProfileInputs.default_namespace.value = 'staging';
    let calls = 0;
    context.fetch = async () => {calls++; return response({}, 409);};
    await app.saveMemoryProfile();
    assert.equal(app.memoryProfileInputs.default_namespace.value, 'staging');
    assert.equal(app.memoryProfile.revision, 1);
    assert.equal(app.memorySaveBtn.disabled, true);
    await app.saveMemoryProfile();
    assert.equal(calls, 1);
    context.fetch = async () => response({data: data({default_namespace:'prod-other'}, 2)});
    await app.loadMemoryProfile();
    assert.equal(app.memoryProfileInputs.default_namespace.value, 'prod-other');
    assert.equal(app.memorySaveBtn.disabled, false);
});

test('422 displays validation feedback without erasing edits', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data());
    app.memoryProfileInputs.default_namespace.value = 'bad namespace';
    context.fetch = async () => response({detail: [{loc:['body','changes','default_namespace'], msg:'invalid namespace'}]}, 422);
    await app.saveMemoryProfile();
    assert.equal(app.memoryProfileInputs.default_namespace.value, 'bad namespace');
    assert.equal(app.memoryProfileStatus.dataset.state, 'error');
    assert.match(app.memoryProfileStatusText.textContent, /默认命名空间/);
    assert.equal(app.memorySaveBtn.disabled, false);
});

test('duplicate submits only send one PATCH', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data());
    let complete, calls = 0;
    context.fetch = () => {calls++; return new Promise(resolve => {complete = resolve;});};
    const pending = app.saveMemoryProfile();
    await app.saveMemoryProfile();
    assert.equal(calls, 1);
    complete(response({data: data()}));
    await pending;
});

test('old account result cannot populate a new account profile', async () => {
    const {app, context} = fixture();
    let complete;
    context.fetch = () => new Promise(resolve => {complete = resolve;});
    const pending = app.loadMemoryProfile();
    app.clearAuthState();
    app.currentUser = {user_id: 'dev_bob'};
    app.accessToken = 'bob';
    complete(response({data: data({default_namespace:'alice-private'})}));
    await pending;
    assert.equal(app.memoryProfile, null);
    assert.equal(app.memoryProfileInputs.default_namespace.value, '');
    assert.equal(app.memoryDialog.open, false);
});

test('late response after close cannot repopulate cleared form', async () => {
    const {app, context} = fixture();
    let complete;
    context.fetch = () => new Promise(resolve => {complete = resolve;});
    const pending = app.loadMemoryProfile();
    app.resetMemoryProfileView();
    complete(response({data: data({default_namespace:'private'})}));
    await pending;
    assert.equal(app.memoryProfile, null);
    assert.equal(app.memoryProfileInputs.default_namespace.value, '');
});

test('401 closes profile dialog and clears prior account fields', async () => {
    const {app, context} = fixture();
    app.applyMemoryProfile(data({default_namespace:'private'}));
    context.fetch = async () => response({}, 401);
    await app.loadMemoryProfile();
    assert.equal(app.currentUser, null);
    assert.equal(app.memoryProfile, null);
    assert.equal(app.memoryDialog.open, false);
    assert.equal(app.memoryProfileInputs.default_namespace.value, '');
});

test('load failure keeps save disabled when no valid revision was read', async () => {
    const {app, context} = fixture();
    context.fetch = async () => response({}, 500);
    await app.loadMemoryProfile();
    assert.equal(app.memoryProfile, null);
    assert.equal(app.memorySaveBtn.disabled, true);
    assert.equal(app.memoryReloadBtn.disabled, false);
});

test('memory badge renders only display metadata as text, including an HTML-like incident title', () => {
    const {app, context} = fixture();
    const created = [];
    context.document.createElement = () => {
        const element = {append(...children) { this.children = children; }};
        created.push(element);
        return element;
    };
    const wrapper = {querySelector: () => null, prepend(element) { this.first = element; }};
    app.renderMemoryBadge({querySelector: () => wrapper}, {
        status: 'degraded', reason: 'vector_unavailable', profile_revision: 3,
        incidents: [{title: '<img src=x onerror=alert(1)>', revision: 2}],
    });
    assert.equal(wrapper.first.children[0].textContent, '部分记忆暂不可用');
    assert.match(wrapper.first.children[1].textContent, /<img src=x onerror=alert\(1\)>（版本 2）/);
    assert.ok(created.every(node => !Object.hasOwn(node, 'innerHTML')));
});
