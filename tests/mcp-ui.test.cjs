const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function createMinimalDom() {
    const listeners = {};
    const elements = {};

    function createElement(id, tag = 'div') {
        const el = {
            id,
            tagName: tag.toUpperCase(),
            hidden: false,
            attributes: {},
            innerHTML: '',
            value: '',
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                toggle(c, force) {
                    if (force !== undefined) {
                        force ? this.classes.add(c) : this.classes.delete(c);
                    } else {
                        this.classes.has(c) ? this.classes.delete(c) : this.classes.add(c);
                    }
                },
                contains(c) { return this.classes.has(c); }
            },
            setAttribute(k, v) { this.attributes[k] = String(v); },
            getAttribute(k) { return this.attributes[k]; },
            removeAttribute(k) { delete this.attributes[k]; },
            addEventListener(event, fn) {
                listeners[`${id}:${event}`] = fn;
            },
            showModal() { this.open = true; },
            close() { this.open = false; },
            reset() { this.value = ''; }
        };
        elements[id] = el;
        return el;
    }

    // Required IDs for MCP Manager
    [
        'mcpConfigDialog', 'mcpConfigBtn', 'closeMcpConfigBtn',
        'mcpServersTab', 'mcpAddTab', 'mcpJsonTab', 'mcpToolsTab',
        'mcpServersPane', 'mcpAddPane', 'mcpJsonPane', 'mcpToolsPane',
        'mcpTransport', 'mcpStdioFields', 'mcpHttpFields',
        'mcpAddForm', 'mcpAddCancelBtn', 'mcpSaveJsonBtn',
        'mcpFormatJsonBtn', 'mcpResetJsonBtn', 'mcpRefreshAllBtn',
        'mcpToolSearchInput', 'mcpServersList', 'mcpJsonEditor',
        'mcpToolsList', 'mcpServerName', 'mcpCommand', 'mcpArgs', 'mcpUrl'
    ].forEach(id => createElement(id));

    const document = {
        getElementById(id) { return elements[id] || null; },
        addEventListener(event, fn) { listeners[`doc:${event}`] = fn; },
        readyState: 'complete'
    };

    return { document, elements, listeners };
}

test('MCP Manager initializes, switches tabs, and formats JSON', async () => {
    const { document, elements, listeners } = createMinimalDom();
    const scriptCode = fs.readFileSync(path.join(__dirname, '../static/mcp-manager.js'), 'utf-8');

    const context = {
        document,
        window: {
            authFetch: async (url, opts) => ({
                ok: true,
                json: async () => ({
                    code: 200,
                    data: {
                        raw: '{"mcpServers":{}}',
                        parsed: { mcpServers: {} },
                        status: { servers: [] }
                    }
                })
            })
        },
        console
    };

    vm.createContext(context);
    vm.runInContext(scriptCode, context);

    const mcpManager = context.window.McpManager;
    assert.ok(mcpManager, 'McpManager should be exposed on window');

    // Test tab switching
    mcpManager.switchTab('json');
    assert.equal(elements.mcpJsonPane.hidden, false);
    assert.equal(elements.mcpServersPane.hidden, true);

    mcpManager.switchTab('servers');
    assert.equal(elements.mcpServersPane.hidden, false);
    assert.equal(elements.mcpJsonPane.hidden, true);

    // Test JSON formatting
    elements.mcpJsonEditor.value = '{"mcpServers":{"test":{"command":"python"}}}';
    mcpManager.handleFormatJson();
    assert.ok(elements.mcpJsonEditor.value.includes('\n'), 'Formatted JSON should contain newlines');
});
