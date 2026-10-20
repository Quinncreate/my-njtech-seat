'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {once} = require('node:events');
const {sessionHeaders} = require('../browser_headers');

test('captured browser headers produce one JSON Content-Type on the wire', async () => {
  const observed = [];
  const server = http.createServer((req, res) => {
    observed.push(req.headers);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({jsonAccepted: req.headers['content-type'] === 'application/json'}));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const url = `http://127.0.0.1:${server.address().port}/graphql`;
    for (const source of [
      {'Content-Type': 'application/json', 'App-Version': 'test'},
      {'Content-Type': 'text/plain', 'content-type': 'application/json', 'App-Version': 'test'},
      {'CONTENT-TYPE': 'application/json', Cookie: 'demo', Origin: 'https://example.invalid',
       Referer: 'https://example.invalid/', 'Sec-Fetch-Site': 'same-origin', 'App-Version': 'test'},
    ]) {
      const response = await fetch(url, {method: 'POST', headers: sessionHeaders(source), body: '{}'});
      assert.equal((await response.json()).jsonAccepted, true);
      const headers = observed.at(-1);
      assert.equal(headers['app-version'], 'test');
      assert.equal(headers.cookie, undefined);
      assert.equal(headers.origin, undefined);
      assert.equal(headers.referer, undefined);
    }
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
