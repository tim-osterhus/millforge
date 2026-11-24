# Third-party notices

Millforge is Apache-2.0. The bundled optimizer archive includes its own
Apache-2.0 license. The separate Python R1 sidecar carries its Nano RLM notice
and license. The historical PyPI package retains its own vendored notices.

The Node package declares `@earendil-works/pi-coding-agent` and `@earendil-works/pi-ai` exactly 1.4.0, source `4a080252f06caf63a525d7873c5d94d3c23887e0` / tag v1.4.0. The M2 run worker imports their selected public APIs; pure profile/inspect commands do not load Pi. Their actual tarballs match the specification's SHA-512 integrities. The verified exact package-lock records transitive identities and integrities; the M0 evidence inventory records package license metadata and embedded notice-member hashes, including optional platform dependencies.

The source/provenance association was checked from registry SLSA statement payloads; cryptographic signature verification was not performed. Both Pi archives omit an embedded LICENSE, so the exact upstream source LICENSE is preserved below (SHA-256 `0457f5bcec3b3b211605dfb5d1a49042fd638f3686a410fe099c24a25af13c48`). The complete frozen lock inventory records 67 entries without an embedded license/notice member across production, development and optional entries. The M3a non-dev metadata inventory narrows that observation to 13 production entries. An absent embedded member alone establishes neither a legal defect nor completeness of redistribution notices. This document records packaging evidence, not redistribution certification.

Dev-only dependencies are TypeScript 5.9.3, Vitest 3.2.4 and @types/node 22.19.0 with their locked closure. They are excluded from the authored package file list.

## Frozen production dependency inventory

The M3a preflight matched 121 non-dev frozen-lock locations, covering 120 distinct tarballs, to the retained byte-verified inventory by version, integrity and resolved URL. Package metadata reports these license labels:

| License metadata | Lock locations |
| --- | ---: |
| MIT | 59 |
| Apache-2.0 | 38 |
| BSD-3-Clause | 13 |
| ISC | 7 |
| BlueOak-1.0.0 | 2 |
| Unlicense | 1 |
| 0BSD | 1 |

Exact rows and notice-member hashes are in `lab/tasks/millforge-m3/2026-11-05/checks/production-license-inventory-preflight.json`. These counts describe the frozen non-dev lock, not an already qualified installed platform closure. M3a must compare the actual installed dependencies and archived bytes before accepting that artifact. The package does not carry the workspace package-lock; direct Pi pins alone do not establish the transitive installed identity. The codemode integration keeps these dependency pins and the lock unchanged.

## Pinned Pi upstream license

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Already locked codemode components

Opt-in codemode uses `@earendil-works/pi-codemode` 1.4.0 through Pi's public factory. It comes from the same pinned Pi source and reports MIT. Its installed archive has no separate LICENSE member; the pinned Pi upstream license above supplies that notice. Millforge adds no direct dependency on this package.

`quickjs-wasi` 3.6.2 is already locked beneath the Pi packages. Its package metadata identifies `vercel-labs/quickjs-wasi` as its source repository. The actual installed `LICENSE` below has SHA-256 `f7735da3b2531ddf451937117343cfadb853fd8054b499e7a70695dc57ffd184`. The installed `quickjs.wasm` has SHA-256 `d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9`. These identities were read from the reused pinned dependency tree. The text was verified directly, beyond the package's MIT metadata.

```text
MIT License

Copyright (c) 2026 Vercel, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
