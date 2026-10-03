# Vendored third-party code

This folder holds code that is **not** ours and is committed as-is so the app
works with no network access and no build step.

| File | Project | Version | Licence |
| --- | --- | --- | --- |
| `pdf.min.js` | [PDF.js](https://mozilla.github.io/pdf.js/) | 2023 build | Apache-2.0 |
| `pdf.worker.min.js` | [PDF.js](https://mozilla.github.io/pdf.js/) | 2023 build | Apache-2.0 |

PDF.js is © Mozilla Foundation. Its licence notice travels inside each file
(`@licstart` … `@licend`); the full text is also in
[LICENSE.pdfjs](LICENSE.pdfjs).

Woodshed Studio itself is MIT licensed (see the `LICENSE` file one level up).
The vendored files are **not** covered by that MIT grant — they remain under
Apache-2.0, which requires keeping their notice intact. That is why the
`.min.js` files are committed verbatim rather than minified further or
processed.

## Why vendored rather than a CDN

The Reader loads PDF.js from here rather than from a CDN so the app has no
outbound network dependency at all: a workshop machine with no internet still
opens PDFs.