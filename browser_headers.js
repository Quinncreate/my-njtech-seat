'use strict';

// These headers are used only in memory inside the isolated school session.
function sessionHeaders(source) {
  const headers = {};
  for (const [name, value] of Object.entries(source || {})) {
    const key = name.toLowerCase();
    if (/^(cookie|host|origin|referer|content-length|connection|accept-encoding|user-agent|content-type|sec-.*|:.*)$/.test(key)) continue;
    headers[key] = value;
  }
  // Fetch merges differently cased duplicate names into a comma-separated value.
  // The school GraphQL endpoint accepts exactly one JSON Content-Type.
  headers['content-type'] = 'application/json';
  return headers;
}

module.exports = {sessionHeaders};
