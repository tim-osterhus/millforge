# Python R1 third-party notice

`nano_output.py` adapts `OutputCapture`, `OutputSize` and `_char_start` from
Nano RLM `rlm` 0.1.0 at commit `8f228311267cc694ecb25f63be2f14ca87a344ca`.
The adaptation uses the caller's UTF-8 byte budget for retained text and its
omission marker. It accepts ordered text from the Millforge bridge and keeps
no output files. Nano RLM is copyright 2026 Prime Intellect and is available
under the MIT terms in `LICENSE.nano-rlm`.

- [Pinned output helper](https://github.com/PrimeIntellect-ai/nano-rlm/blob/8f228311267cc694ecb25f63be2f14ca87a344ca/src/rlm/tools/output.py)
- [Pinned MIT license](https://github.com/PrimeIntellect-ai/nano-rlm/blob/8f228311267cc694ecb25f63be2f14ca87a344ca/LICENSE)
