'use strict';

// Contract-only client tests: no real cloud calls or filesystem writes.
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const filename = path.resolve(__dirname, '../../../miniprogram/pages/clozeBuilder/clozeExportService.js');
const source = fs.readFileSync(filename, 'utf8');

function fixture({ variant = 'combined', format = 'docx', ticket, files } = {}) {
  const requests = []; const containerCalls = []; const writes = [];
  const envelope = ticket || { request: { format, variant }, expiresAt: 2000000300000, signature: 'signed-server-payload' };
  const output = files || [{ name: '打印测试-' + variant + '.' + format, variant, format, contentBase64: 'AQ==', size: 1 }];
  const sandbox = {
    module: { exports: {} },
    require: id => {
      if (id === './clozeWordStyle') return require('../../../miniprogram/pages/clozeBuilder/clozeWordStyle');
      assert.equal(id, '../../services/cloudContainer');
      return { callContainer: async args => { containerCalls.push(args); return { statusCode: 200, data: { success: true, data: { files: output } } }; } };
    },
    wx: {
      env: { USER_DATA_PATH: '/mock-path-no-real-write' },
      cloud: { callFunction: async args => { requests.push(args); return { result: { success: true, data: envelope } }; } },
      getFileSystemManager: () => ({ writeFile: args => { writes.push(args); args.success(); } }),
    },
  };
  vm.runInNewContext(source, sandbox, { filename });
  return { service: sandbox.module.exports, requests, containerCalls, writes, envelope };
}

async function main() {
  const wordStyle = {fontFamily:'KaiTi',fontSize:10.5,showTitle:false};
  const typography = fixture({ticket:{request:{format:'docx',variant:'combined',wordStyle},signature:'signed'}});
  await typography.service.exportClozeDocument('owned-document',{wordStyle});
  assert.deepEqual(JSON.parse(JSON.stringify(typography.requests[0].data.wordStyle)),wordStyle);
  const old = fixture();
  await assert.rejects(old.service.exportClozeDocument('owned-document',{wordStyle}),/字体设置/);
  assert.equal(old.containerCalls.length,0);
  for (const bad of [{fontFamily:'Comic Sans'},{fontSize:0},{fontSize:99},{fontSize:'12'},{showTitle:'false'}]) {
    const f=fixture(); await assert.rejects(f.service.exportClozeDocument('owned-document',{wordStyle:bad})); assert.equal(f.requests.length,0);
  }
  for (const format of ['docx', 'pdf']) for (const variant of ['practice', 'answers', 'combined']) {
    const test = fixture({ format, variant });
    const output = await test.service.exportClozeDocument('owned-document', { format, variant });
    assert.equal(test.requests[0].data.variant, variant); assert.equal(test.requests[0].data.format, format);
    assert.equal(test.containerCalls[0].data, test.envelope, 'The signed envelope is forwarded unchanged');
    assert.equal(test.containerCalls[0].path, '/cloze-export');
    assert.equal(test.writes.length, 1); assert.equal(output.variant, variant); assert.equal(output.format, format);
    assert(output.path.endsWith('.' + format));
  }
  const legacy = fixture({ ticket: { request: { format: 'docx' }, expiresAt: 2000000300000, signature: 'legacy' }, files: [{ name: 'old.docx', contentBase64: 'AQ==', size: 1 }] });
  await legacy.service.exportClozeDocument('old-document');
  assert(!Object.hasOwn(legacy.requests[0].data, 'variant'));
  assert(!Object.hasOwn(legacy.requests[0].data, 'format'));
  assert(!Object.hasOwn(legacy.service.buildPayload({ blocks: [] }), 'variant'));
  assert.equal(legacy.service.buildPayload({ blocks: [] }, { variant: 'practice', format: 'pdf' }).variant, 'practice');
  for (const options of [{ variant: 'bad' }, { format: 'html' }, { variant: null }, 'practice', null, []]) {
    const test = fixture();
    await assert.rejects(test.service.exportClozeDocument('id', options));
    assert.equal(test.requests.length, 0);
  }
  for (const options of [{ variant: 'practice' }, { variant: 'answers' }, { format: 'pdf' }]) {
    const test = fixture({ ticket: { request: { format: 'docx' }, signature: 'old-ticket' } });
    await assert.rejects(test.service.exportClozeDocument('id', options), /尚未支持/);
    assert.equal(test.containerCalls.length, 0, 'An old signed combined ticket cannot export an answer-leaking practice file');
    assert.equal(test.writes.length, 0);
  }
  for (const files of [
    [{ name: 'wrong.docx', variant: 'combined', contentBase64: 'AQ==' }],
    [{ name: 'wrong.pdf', variant: 'practice', contentBase64: 'AQ==' }],
    [{ name: 'empty.docx', variant: 'practice', contentBase64: '' }],
    [{ name: 'practice.docx', variant: 'practice', contentBase64: 'AQ==' }, { name: 'answers.docx', variant: 'answers', contentBase64: 'AQ==' }],
  ]) {
    const test = fixture({ variant: 'practice', files });
    await assert.rejects(test.service.exportClozeDocument('id', { variant: 'practice' }));
    assert.equal(test.writes.length, 0, 'Unexpected variants/files are rejected before saving');
  }
  console.log('Cloze export client: six variants, unchanged signed envelopes, legacy compatibility and fail-closed file guards passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
