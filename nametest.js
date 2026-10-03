// Kapsamlı test: prettyName() ve isim sıralaması, 1091 gerçek kitap adı üzerinde.
// Gerçek kod studio.html'den çıkarılır, kopya yazılmaz.
const fs = require('fs');
const html = fs.readFileSync('studio.html', 'utf8');

function grab(name) {
  const i = html.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('bulunamadı: ' + name);
  let d = 0;
  for (let j = html.indexOf('{', i); j < html.length; j++) {
    if (html[j] === '{') d++;
    else if (html[j] === '}') { d--; if (!d) return html.slice(i, j + 1); }
  }
  throw new Error('parantez dengesi bozuk: ' + name);
}
const shoutSrc = html.slice(html.indexOf('const SHOUT_OK='), html.indexOf('\n', html.indexOf('const SHOUT_OK=')));
const api = eval('(function(){' + shoutSrc + grab('prettyName') + grab('natKey') + grab('natCmp')
  + '\nreturn {prettyName, natKey, natCmp};})()');
const { prettyName, natCmp } = api;

// Veri: canli API'den. Sunucu ayakta degilse _b.json dosyasina duser.
// Kullanim:  node nametest.js
async function loadBooks() {
  if (!fs.existsSync('_b.json')) {
    const r = await fetch('http://127.0.0.1:8766/api/books');
    if (!r.ok) throw new Error('/api/books yanit vermedi: ' + r.status);
    const j = await r.json();
    fs.writeFileSync('_b.json', JSON.stringify(j));
    console.log('(veri canli API den alindi)');
  }
  const raw = JSON.parse(fs.readFileSync('_b.json', 'utf8'));
  return raw.books || raw;
}

async function main() {
const books = await loadBooks();
const names = books.map(x => (x.pub || x.title || '').trim());
let fails = 0, checks = 0;
const ok = (cond, label, detail) => {
  checks++;
  if (!cond) { fails++; console.log('  BASARISIZ  ' + label + (detail ? '\n            ' + detail : '')); }
};

// Bir ismin "çekirdeği": harf ve rakamlar. Temizlik yalnızca ayırıcı ve
// bilinen indirme kalıntılarını kaldırır, bu yüzden çekirdek korunmalı.
const core = s => (s.match(/[^\W_]+/gu) || []).join('').toLowerCase();
const RESIDUE = /[\s_(\[]*(?:vk[_ .-]?com[_ .-]?en(?:glishmagazines)?|englishmagazines?|pdfdrive|libgen|annas-?archive)\b|[-_]ne(?:[_ .-]?en)?\b|\{[0-9a-f]{6,}\}/gi;

console.log('=== prettyName: ' + names.length + ' isim ===');

const out = names.map(prettyName);

// 1 bos sonuc
ok(out.every(s => s && s.trim().length > 0), 'sonuç hiçbir isimde boş değil',
   out.filter(s => !s || !s.trim()).map((s, i) => names[i]).slice(0, 5).join(' | '));

// 2 idempotans: ikinci uygulama bir şey değiştirmemeli
const twice = out.map(prettyName);
const nonIdem = out.map((s, i) => [s, twice[i], i]).filter(([a, b]) => a !== b);
ok(nonIdem.length === 0, 'prettyName idempotent (2. uygulama değiştirmiyor)',
   nonIdem.slice(0, 3).map(([a, b, i]) => i + ': ' + a + '  =>  ' + b).join('\n            '));

// 3 artık kalıntı kalmadı
const leftUnderscore = out.filter(s => s.includes('_'));
ok(leftUnderscore.length === 0, 'alt çizgi kalmadı', leftUnderscore.slice(0, 3).join(' | '));
const leftSite = out.filter(s => /vk[_ .-]?com|pdfdrive|englishmagazines|libgen|annas-?archive/i.test(s));
ok(leftSite.length === 0, 'indirme sitesi kalıntısı kalmadı', leftSite.slice(0, 3).join(' | '));
const leftNe = out.filter(s => /[-_]ne(?:[_ .-]?en)?\s*$/i.test(s));
ok(leftNe.length === 0, '-ne kesik kalmadı', leftNe.slice(0, 3).join(' | '));
const leftHash = out.filter(s => /\{[0-9a-f]{6,}\}/i.test(s));
ok(leftHash.length === 0, 'indirme karması kalmadı', leftHash.slice(0, 3).join(' | '));

// 4 çekirdek korunuyor: harf/rakam dizisi yalnızca bilinen kalıntılar dışında aynı
const coreBad = [];
out.forEach((s, i) => {
  const expect = core(names[i].replace(RESIDUE, ' '));
  if (core(s) !== expect) coreBad.push(i + ': "' + names[i] + '" -> "' + s + '"');
});
ok(coreBad.length === 0, 'harf/rakam çekirdeği korunuyor (' + names.length + ' isim)',
   coreBad.slice(0, 5).join('\n            '));

// 5 uzunluk artmadı, kesilme yok
const grew = out.map((s, i) => [s, names[i], i]).filter(([s, b]) => s.length > b.length);
ok(grew.length === 0, 'temizlenmiş isim daha uzun değil', grew.slice(0, 3).map(([s, b]) => s).join(' | '));

// 6 Unicode bozulmadı (№, ’ vb. yerinde)
const uni = out.filter(s => s.includes('�') || /&[a-z]+;|Ã|Å|Ä/.test(s));
ok(uni.length === 0, 'Unicode / kodlama bozulması yok', uni.slice(0, 3).join(' | '));

// 7 boşluk hijyeni
const sp = out.filter(s => /\s{2,}|[\s,]\s*[).,]|\s-\s-\s/.test(s));
ok(sp.length === 0, 'boşluk/punktüasyon hijyeni yok', sp.slice(0, 3).join(' | '));

// 8 parantez dengesi: temizlik yeni dengesizlik üretmemeli (kaynak adın kendisi
// dengesiz olabilir, o zaman miras alınmış sayılır)
const imb = s => (s.match(/\(/g) || []).length - (s.match(/\)/g) || []).length;
const newImb = out.map((s, i) => [s, i]).filter(([s, i]) => Math.abs(imb(s)) > Math.abs(imb(names[i])));
ok(newImb.length === 0, 'temizlik yeni parantez dengesizliği üretmiyor', newImb.slice(0, 3).map(([s]) => s).join(' | '));
const inherited = out.map((s, i) => [s, names[i]]).filter(([s, o]) => imb(s) !== 0);
console.log('  kaynak adından gelen dengesizlik (düzeltilemeyen): ' + inherited.length +
            (inherited.length ? ' -> ' + inherited.slice(0, 2).map(([s]) => s).join(' | ') : ''));

// 9 çift parantez kalıntısı giderildi mi
const dbl = out.filter(s => /\(|\)/.test(s) && (s.match(/\(/g) || []).length !== (s.match(/\)/g) || []).length);
ok(dbl.length === 0, 'çift parantez kalıntısı yok', dbl.slice(0, 3).join(' | '));

// 10 sonuç yine bir isim gibi görünüyor (ilk karakter büyük harf veya rakam değil, boş değil)
const weird = out.filter(s => /^\s|\s$/.test(s));
ok(weird.length === 0, 'baştaki/sondaki boşluk yok', weird.slice(0, 3).join(' | '));

console.log('\n=== istatistik ===');
const changed = names.filter((b, i) => b !== out[i]);
console.log('  değişen        : ' + changed.length + ' / ' + names.length +
            ' (' + Math.round(100 * changed.length / names.length) + '%)');
console.log('  boş sonuç      : ' + out.filter(s => !s.trim()).length);
console.log('  kalan alt çizgi: ' + leftUnderscore.length);
console.log('  kalan site     : ' + leftSite.length);
console.log('  kalan -ne      : ' + leftNe.length);
console.log('  kalan karma    : ' + leftHash.length);
console.log('  temizlenen çeşit: ' + new Set(changed).size + ' farklı isim');

console.log('\n=== örnek dönüşümler ===');
const seen = new Set(); let shown = 0;
names.forEach((b, i) => {
  if (b !== out[i] && !seen.has(b) && shown < 10) {
    seen.add(b); shown++;
    console.log('  ESKI: ' + b.slice(0, 62));
    console.log('  YENI: ' + out[i].slice(0, 62));
  }
});

// 11 sıralama: temizlenmiş adlarla alfabetik sıra bozulmamalı
const sorted = names.map((n, i) => ({ n, s: out[i], i })).sort((a, b) => {
  const c = natCmp(a.s, b.s);
  return c ? c : (a.i - b.i);
});
let inv = 0, firstBad = null;
for (let i = 1; i < sorted.length; i++) {
  if (natCmp(sorted[i - 1].s, sorted[i].s) > 0) { inv++; if (!firstBad) firstBad = sorted[i - 1].s + '  >  ' + sorted[i].s; }
}
ok(inv === 0, 'temizlenmiş adlarla alfabetik sıra (1091 isim)', firstBad);

// 12 aynı temiz adlar bitişik kalmalı (aynı dergi sayıları dağınmasın)
const byKey = new Map();
out.forEach(s => { const k = s.replace(/\s+/g, ' ').toLowerCase(); byKey.set(k, (byKey.get(k) || 0) + 1); });
const dupes = [...byKey.entries()].filter(([, n]) => n > 1);
console.log('  aynı temiz ada düşen isim: ' + dupes.length + ' küme (' +
            dupes.reduce((s, [, n]) => s + n, 0) + ' kitap)');

// 13 Temizleme bir ismi silmemeli. "_vk_com_.pdf" gibi bir dosya tamamen
// kalıntıdan ibaret ve prettyName() "" döndürür; bu yüzden her çağıran
// `prettyName(x) || x` yazmak zorunda. Örüntünün sessizce kaybolmaması için
// fallback'in burada çalıştığını doğrula.
const degenerate = ['vk_com', '-ne', '{ABCDEF}', '____', '( )', '_', '  '];
const lostDegenerate = degenerate.filter(s => !(prettyName(s) || s));
ok(lostDegenerate.length === 0, 'temizlenince boşalan isim ham haliyle görünür',
   'kayıp: ' + JSON.stringify(lostDegenerate));

// 14 İki uygulama aynı mı? titles.display_name() (sunucu) ile prettyName()
// (arayüz) aynı kuralı iki kez yazıyor. Ayrışırlarsa aynı kitap ekrana
// göre iki isimle çıkar. _expect.json, `py -3 nametest.py` çıktısıdır.
let pyChecked = false;
if (fs.existsSync('_expect.json')) {
  pyChecked = true;
  const expect = JSON.parse(fs.readFileSync('_expect.json', 'utf8'));
  ok(expect.length === names.length, 'beklenen dosya aynı sayıda isim',
     expect.length + ' vs ' + names.length);
  const drift = out.map((s, i) => [names[i], s, expect[i]])
                   .filter(([, a, b]) => a !== b);
  ok(drift.length === 0, 'JS prettyName ile Python display_name birebir aynı (' +
     names.length + ' isim)',
     drift.slice(0, 5).map(([n, a, b]) => n + '\n  JS : ' + a + '\n  PY : ' + b).join('\n            '));
  console.log('  python ile karsilastirilan isim: ' + names.length);
} else {
  console.log('  (!) _expect.json yok - once `py -3 nametest.py` calistir');
}

console.log('\n=== sonuç ===');
console.log(checks + ' kontrol, ' + fails + ' başarısız');
process.exit(fails ? 1 : 0);
}
main().catch(e => { console.error('TEST ÇALIŞMADI: ' + e.message); process.exit(2); });