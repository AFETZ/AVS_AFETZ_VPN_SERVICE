// Browser-side import contract, runnable without a phone: node tests/openflux_import_page.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/web/templates/openflux_import.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
function page(fragment, android = true, clipboardFails = false) {
  const elements = {};
  for (const id of ['status','open','copy','manual','help']) elements[id] = {hidden:true, addEventListener(_, fn){this.click = fn;}};
  const copied = [];
  const location = {hash:'#'+fragment, href:'https://link.example/connection?client=openflux#'+fragment};
  const navigator = {userAgent:android ? 'Android' : 'iPhone', clipboard:{writeText:async value => {
    if (clipboardFails) throw Error('clipboard unavailable'); copied.push(value);
  }}};
  vm.runInNewContext(script, {document:{getElementById:id => elements[id]},location,navigator,URL,URLSearchParams});
  return {elements, location, copied};
}
(async () => {
  const doc = 'https://disk.yandex.ru/i/test?a=1&b=%2B+#part';
  const fragment = new URLSearchParams({version:'1',document_url:doc}).toString();
  for (const android of [true, false]) {
    const p = page(fragment, android);
    const link = new URL(p.location.href);
    assert.equal(link.protocol, 'afzopenflux:');
    assert.equal(link.hostname, 'import');
    assert.equal(link.searchParams.get('document_url'), doc);
    assert.equal(p.elements.open.hidden, false);
    if (android) assert.match(p.elements.open.href, /package=uk\.superbebra\.afzopenflux;end$/);
    else assert.equal(p.elements.open.href, p.location.href);
    await p.elements.copy.click();
    assert.deepEqual(p.copied, [doc]);
  }
  const fallback = page(fragment, true, true);
  await fallback.elements.copy.click();
  assert.equal(fallback.elements.manual.hidden, false);
  assert.equal(fallback.elements.manual.textContent, doc);
  for (const invalid of ['', 'version=2&document_url='+encodeURIComponent(doc), fragment+'&version=1',
      fragment+'&document_url='+encodeURIComponent(doc), 'version=1&document_url=javascript%3Aalert(1)',
      'version=1&document_url='+encodeURIComponent('https://disk.yandex.ru.evil.test/i/x'),
      'version=1&document_url='+encodeURIComponent('https://disk.yandex.ru\\@evil.test/i/x'), 'x'.repeat(16385)]) {
    const p = page(invalid);
    assert.match(p.location.href, /^https:/);
    assert.equal(p.elements.open.hidden, true);
    assert.equal(p.elements.copy.hidden, true);
  }
  console.log('Launcher: Android/iOS links, exact profile, clipboard fallback and 8 invalid inputs passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
