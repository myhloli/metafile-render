# Changelog

## 0.4.0

### Compatibility

- Require GIL-enabled CPython 3.11–3.14 and the bundled Rust extension.
- Publish `cp311-abi3` wheels for Linux x86-64/aarch64 (glibc 2.28+), Windows x64/ARM64, and macOS 14+ arm64.
- Stop publishing pure Python wheels and source distributions. Python 3.10, free-threaded CPython, PyPy, Intel macOS, and musl are outside this release policy. Building from the repository requires Rust.
- Keep the public rendering API and `backend="auto"` / `backend="replay"` choices unchanged.
- Require pyclipper 1.3.0.post6+ on CPython 3.11–3.13, and 1.4.0+ on 3.14; older generated extension sources do not support the selected Python range.

### Rendering and performance

- Decode and transform common EMF point batches in Rust while preserving GDI state, diagnostics, and resource limits.
- Reuse record-local mapping matrices, skip identity clip operations, and cache SVG number formatting within each render.
- Limit large-canvas composition, opaque solid fills, and opaque strokes to their coverage regions; bound temporary image storage.
- Preserve encoded outputs, fallback pixels, diagnostics, partial rendering, and rejection behavior against frozen references.

On macOS arm64, a paired comparison with the published 0.3.1 package reduced complete `docx_01.doc` parsing time from about 951 ms to 650 ms (31.7%). Its 39 metafiles took about 851 ms versus 539 ms (36.3% less time). The third optimization round reduced the expanded primary corpus time by 29.1% relative to the previous candidate, including 41.6% for WMF, and reduced the whole-corpus process RSS peak by 19.4%. These are separate comparisons and are not additive.

### Validation tools

- Add a pinned 90-input EMF/WMF corpus with source hashes and distinct primary, generated-control, and robustness groups.
- Add independent-process AB/BA timing, RSS, full-output verification, and strict five-platform native-wheel release checks.
- Validate 180 SVG/PNG combinations and isolated CPython 3.11/3.14 wheel installations. Local tests pass 618 cases; Windows GDI checks run in Windows CI.
