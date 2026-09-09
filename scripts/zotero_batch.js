// Executed once in Zotero's built-in Run JavaScript window. `request` is generated
// by zotero_gui.py, never read from a webpage. No HTTP endpoint is registered.
const state = {run_id: request.run_id, status: 'running', action: request.action, started_at_ms: Date.now(), rows: []};
async function checkpoint() {
    await Zotero.File.putContentsAsync(request.report, JSON.stringify(state));
}
try {
    if (!['probe', 'find', 'attach'].includes(request.action)) throw Error('Invalid action');
    const collection = Zotero.Collections.getByLibraryAndKey(1, request.collection);
    if (!collection || !collection.library.editable) throw Error('Missing editable target collection');
    await checkpoint();
    for (const entry of request.items) {
        const item = Zotero.Items.getByLibraryAndKey(1, entry.key);
        if (!item || !item.isRegularItem() || !item.inCollection(collection.id)) {
            throw Error('Item outside exact target collection: ' + entry.key);
        }
        if (item.getField('DOI').toLowerCase() !== entry.doi.toLowerCase()
            || item.getField('title') !== entry.title) throw Error('Item identity changed');
        const row = {key: item.key, doi: entry.doi, status: 'started', started_at_ms: Date.now()};
        state.rows.push(row);
        await checkpoint();
        try {
            let existing = [];
            for (const id of item.getAttachments()) {
                const a = Zotero.Items.get(id);
                if (a.attachmentContentType === 'application/pdf') {
                    existing.push({key: a.key, exists: await a.fileExists()});
                }
            }
            if (request.action === 'probe') {
                row.status = 'probed';
            } else if (existing.length) {
                // Do not duplicate, silently delete, or replace suspect attachments.
                row.status = 'existing_pdf_needs_validation';
            } else if (request.action === 'find') {
                // Explicit methods: no user/custom mirror resolvers, no access bypass.
                const a = await Zotero.Attachments.addAvailableFile(item, {methods: ['doi', 'url', 'oa']});
                row.status = a ? 'found_needs_validation' : 'not_found';
                if (a) row.attachment_key = a.key;
            } else {
                if (!/^http:\/\/127\.0\.0\.1:\d+\/[a-f0-9]+\.pdf$/.test(entry.stream_url)) {
                    throw Error('Attachment requires a scoped loopback stream');
                }
                const a = await Zotero.Attachments.importFromURL({url: entry.stream_url,
                    parentItemID: item.id, contentType: 'application/pdf', title: entry.attachment_title || 'Full Text PDF'});
                a.setField('url', entry.source_url);
                await a.saveTx();
                row.status = 'attached_needs_validation';
                row.attachment_key = a.key;
            }
            row.existing = existing;
        } catch (e) {
            row.status = 'error';
            row.error = String(e.message || e);
        }
        row.finished_at_ms = Date.now();
        row.seconds = (row.finished_at_ms - row.started_at_ms) / 1000;
        await checkpoint();
    }
    state.status = 'complete';
} catch (e) {
    state.status = 'error';
    state.error = String(e.message || e);
}
state.finished_at_ms = Date.now();
state.seconds = (state.finished_at_ms - state.started_at_ms) / 1000;
await checkpoint();
return JSON.stringify(state);
