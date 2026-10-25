# Technical reference

API contracts, rendering behavior, and contributor workflows for Metafile Render.
For installation and a first conversion, start with the
[English README](https://github.com/myhloli/metafile-render/blob/main/README.md) or
[中文 README](https://github.com/myhloli/metafile-render/blob/main/README_zh-CN.md).

## Contents

- [Requirements](#requirements)
- [Python API](#python-api)
- [Command line](#command-line)
- [Output formats](#output-formats)
- [Backend selection](#backend-selection)
- [Rendering and fonts](#rendering-and-fonts)
- [Resource limits](#resource-limits)
- [Development](#development)
- [Internal architecture](#internal-architecture)
- [Benchmarks and fixtures](#benchmarks-and-fixtures)
- [Publishing](#publishing)

## Requirements

GIL-enabled **CPython 3.11–3.14** on the supported native-wheel platforms. No Office installation or
external conversion executable is required. The runtime dependencies are:

| Python | Pillow | pyclipper |
| :--- | :--- | :--- |
| 3.11–3.13 | `>=11.0.0` | `>=1.3.0.post6,<2` |
| 3.14 | `>=12.0.0` | `>=1.4.0,<2` |

Compatible installed dependencies can be retained; these ranges do not pin a
fresh installation to older releases.

## Python API

```python
from pathlib import Path
from metafile_render import MetafileError, render_metafile

try:
    result = render_metafile(
        Path("input.emf").read_bytes(),
        output_format="svg",
        dpi=None,  # defaults to 200 for every output format
        size_hint=None,
        backend="auto",  # use "replay" to force the portable engine
    )
except MetafileError as error:
    print(error.code, str(error))
else:
    Path("output.svg").write_bytes(result.data)
    print(result.width, result.height, result.media_type, result.partial)
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message)
```

`render_metafile(data: bytes, *, output_format="png", dpi=None, size_hint=None, backend="auto")`
returns a `MetafileRenderResult`. Omitted or `None` DPI means **200 DPI for SVG,
PNG, JPEG, and WebP**. An explicit `dpi` must be an integer from 1 through 1200;
`size_hint`, when supplied, is a pair of positive integer pixel dimensions, useful
for standard WMF images without physical dimensions. Resource limits may reduce
the actual canvas size.

`data` must be `bytes`. A supplied `size_hint` takes precedence over dimensions
derived from the file and DPI, subject to canvas and render-work limits.

When the requested canvas exceeds the render-work budget, it is proportionally
downscaled (rounding down to whole pixels, with a minimum of one pixel per side).
Work units count drawing commands plus their clip operations. The actual canvas
satisfies both the canvas limits and `width * height * work_units <= 512_000_000`.
The `canvas_downscaled` warning reports requested and actual dimensions and the
budget reason. Downscaling alone does not set `partial`; existing content warnings
remain. All formats and native/replay backends use this sizing policy. DPI metadata
keeps the requested value, as with existing canvas downscaling. Input limits and
workloads that cannot fit even a 1×1 canvas still raise resource-limit errors.

The returned `MetafileRenderResult` is an immutable dataclass:

| Field | Value |
| :--- | :--- |
| `data` | Encoded image bytes |
| `output_format` | `svg`, `png`, `jpeg`, or `webp` |
| `media_type` | `image/svg+xml`, `image/png`, `image/jpeg`, or `image/webp` |
| `width`, `height` | Actual output dimensions in pixels |
| `source_format` | `wmf` or `emf` |
| `emfplus_mode` | `none`, `only`, or `dual` |
| `partial` | Whether content was approximated or skipped |
| `diagnostics` | Tuple of immutable `MetafileDiagnostic` objects |

Each diagnostic has `code`, `level` (`info`, `warning`, or `error`), and `message`.
Its optional `record_type`, `record_index`, and byte `offset` locate the source
record; unavailable locations are `None`.

Public exports are `render_metafile`, `MetafileOutputFormat`,
`MetafileBackend`, `MetafileRenderResult`, `MetafileDiagnostic`, `MetafileError`,
`MetafileMalformedError`, `MetafileResourceLimitError`, and `MetafileUnsupportedError`.
The parser, drawing models, and renderer internals are not a stable public API.
Invalid API arguments raise `TypeError` or `ValueError`; malformed, unsupported,
and over-budget images raise the corresponding `MetafileError` subclass.

| Exception | `code` | Meaning |
| :--- | :--- | :--- |
| `MetafileMalformedError` | `malformed` | Invalid metafile structure |
| `MetafileUnsupportedError` | `unsupported` | Unsupported input or unavailable output encoder |
| `MetafileResourceLimitError` | `resource_limit` | Input or rendering exceeds a fixed resource budget |

Catch `MetafileError` to handle all three; file reads and writes may separately
raise `OSError`. API format names use `jpeg`; `jpg` is only a CLI file extension.

## Command line

```bash
metafile-render input.emf -o output.svg
metafile-render input.wmf -o output.png --dpi 200 --size 800 600
metafile-render input.emf -o output.webp
python -m metafile_render input.emf -o output.jpg --force
metafile-render input.emf -o transparent.png --backend replay
metafile-render --version
```

| Argument | Meaning |
| :--- | :--- |
| `input` | Required source WMF or EMF path |
| `-o`, `--output` | Required destination path; its extension selects the format |
| `--dpi` | Integer from 1 to 1200; defaults to 200 |
| `--size WIDTH HEIGHT` | Two positive integer pixel dimensions, subject to canvas limits |
| `--backend` | `auto` (default) or `replay` |
| `--force` | Replace an existing output file |
| `--version` | Print the installed package version and exit |
| `-h`, `--help` | Print usage and exit |

The output extension selects the format: `.svg`, `.png`, `.jpg`, `.jpeg`, or `.webp`
(case-insensitive). The output directory must exist. Existing output files are
preserved unless `--force` is supplied; input and output must be different files.
Completed outputs are published atomically. Input reads are bounded.

Exit codes: `0` for a completed conversion (including partial rendering), `1` for
conversion or filesystem errors, and `2` for invalid arguments. Diagnostics go to
stderr. Partial rendering is explicitly reported. Use the Python API to inspect
individual diagnostic fields.

## Output formats

Supported output format strings are `svg`, `png`, `jpeg`, and `webp`.
Replay preserves transparency in PNG and WebP; JPEG uses a white background at
quality 90. Native GDI produces opaque white-background images. WebP uses lossy
quality 90, method 4. PNG/JPEG include the selected DPI metadata; WebP uses DPI
for pixel dimensions without adding EXIF resolution metadata.
SVG is self-contained, with embedded images and a PNG fallback in metadata.
Some raster operations require a raster image wrapped in SVG. SVG dimensions
also default to 200 DPI; embedded fallback images may use up to 2× sampling.
Their existing 96 × sampling-factor PNG display-density metadata is independent
of the API’s DPI-to-pixel calculation.

WebP output requires a Pillow build with WebP encoding support, as provided by its
standard wheels; an unavailable encoder raises `MetafileUnsupportedError`.

SVG uses `data-metafile-render="wmf-emf"`, PNG metadata ID
`metafile-render-raster-fallback`, and local clip IDs `metafile-render-clip-N`.
The generated-image marker is not authentication of an arbitrary SVG. Consumers
accepting externally supplied SVG should validate its structure independently.

## Backend selection

`backend="auto"` (the default) keeps SVG on the portable replay engine, including
its embedded PNG fallback. PNG/JPEG/WebP prefer Pillow’s native GDI renderer on
Windows and use replay elsewhere. `backend="replay"` forces portable replay on
all platforms, useful for transparent output and reproducible backend selection.

EMF+ Only and standard WMF without a placeable header go directly to replay:
Pillow’s GDI backend cannot reliably render those streams. Dual uses the EMF
stream and is never drawn twice. Native success does not require the replay
engine to understand every drawing record.

Known record boundaries, payloads and fixed resource limits are checked before
native rendering. Native capability/load failures produce an informational
`native_backend_fallback` diagnostic and retry with replay. Switching backends
does not by itself set `partial=True`. Native GDI returns white-background RGB;
this package does not infer transparency by removing white pixels. No automatic
pixel comparison or blank-image heuristic is used to certify native fidelity.
Pillow 11.0 and later use an isolated instance-size adapter to render directly
into the bounded target canvas; calls are serialized around Pillow’s shared WMF
handler. The native path is GDI, not a new GDI+ backend.

## Rendering and fonts

Placeable and standard WMF and common EMF drawing records are supported.
EMF+ Only supports bounded object definitions (including continued objects), solid
brushes and pens, paths, basic shapes, world/page transforms, Save/Restore and
containers, rectangle/path clipping, compressed PNG/JPEG and common 24/32-bit
RGB/ARGB/PARGB bitmaps, and horizontal Unicode strings with basic alignment.
Unicode driver strings support explicit positions; glyph-index text is skipped.
Only files also replay EMF drawing inside GetDC intervals. EMF+ Dual retains its
existing EMF fallback path and does not draw both streams.

The portable EMF+ engine prioritizes usable content over pixel-identical GDI+ reproduction:

- Linear gradients use the start color; path gradients use the center color;
  hatch brushes use the foreground color. Texture fills without a representative
  color are skipped.
- Font substitutions, text spacing, antialiasing and bitmap sampling can differ
  from native GDI+. Advanced wrapping, trimming and text formatting are approximated.
- Complex Region objects, cardinal splines, nested metafile images, glyph-index or
  vertical text, image effects and custom caps are not fully implemented.
- SourceCopy uses SourceOver. Non-default quality settings use the existing renderer.
- Unsupported objects replace their slots with an unavailable object; stale objects
  are never reused. Unsafe unsupported state changes stop later drawing while the
  remaining record boundaries are still checked.

Approximations and skipped features produce `partial=True` with diagnostic codes,
record types and source offsets. EMF+ Only files with no supported drawing operations
raise `MetafileUnsupportedError`. Malformed structures and resource overflows
continue to raise their specific errors. This is not complete GDI+ compatibility.

Font lookup uses installed system fonts and common aliases, trying matching
bold/italic styles before the regular face, then Pillow's default font.
`font_substituted` informational diagnostics identify replacements at their source
record; font substitution alone does not change `partial`. Install the fonts used by the source document for closer text fidelity;
glyph coverage and measurements can vary across systems. Font files are not bundled.

## Resource limits

Fixed budgets bound input bytes, record and object counts, nesting, geometry,
embedded images, and rendering work. Input is limited to 128 MiB; generated SVG
is limited to 64 MiB; the canvas is limited to 8192 per side and 16 million pixels.

## Development

Run these commands from the repository root:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/metafile_render
.venv/bin/python tools/build_distributions.py dist-native
.venv/bin/python -m twine check dist-native/*.whl
.venv/bin/python tools/check_distributions.py dist-native
```

On Windows, the virtual environment interpreter is `.venv\Scripts\python.exe`.
CI tests CPython 3.11–3.14 on Linux, macOS, and Windows, plus minimum dependency
combinations on Linux and Windows. Windows jobs exercise real GDI rendering.
Core replay tests explicitly select the replay backend. CI also checks static
types and installs native wheels in environments without MinerU. Rust is required
for source development and builds; there is no optional-compilation mode.
Real EMF test images are read from a test-only presentation package dependency.

## Internal architecture

The stable API returns public result/diagnostic models. Header inspection and a
shared bounded record iterator feed separate WMF/EMF record handlers and EMF+
playback. A replay context owns source locations, input budgets and diagnostics;
GetDC state changes are isolated in a GDI bridge. Drawing commands use tagged
image payloads and named text alignment instead of backend-specific placeholders.

Raster, SVG, bitmap, text, path and compositing modules share a render session.
It reuses decoded images and clip masks with a **64 MiB per-conversion LRU limit**,
returns independent copies before mutations, and releases cached images at the
end of conversion. Input budgets and conservative render-work checks apply even
on cache hits. Fill and stroke share flattened paths. Raster supersampling and
SVG fallback selection use the same clip-aware work estimator.

Common EMF PolylineTo and PolyBezierTo records use the required Rust kernel
for batched point decoding, validation and affine transformation. Input point
budgets are checked before native allocation. Python retains GDI state, current
positions, path construction and diagnostics; exceptional records follow the
existing Python path. No public rendering-backend parameter changes are needed.

Native wheels use PyO3's CPython 3.11 stable ABI (`abi3-py311`). The Python
installation floor and wheel ABI floor are both 3.11. Free-threaded CPython,
PyPy and Python 3.10 are outside this release policy.

The release gate requires exactly these five native wheels:

| Platform | Wheel platform tag |
| --- | --- |
| Linux x86-64, glibc 2.28+ | `manylinux_2_28_x86_64` |
| Linux aarch64, glibc 2.28+ | `manylinux_2_28_aarch64` |
| Windows x64 | `win_amd64` |
| Windows ARM64 | `win_arm64` |
| macOS 14+ arm64 | `macosx_14_0_arm64` |

Pure Python wheels and source distributions are not published. Developers can
build from source with Rust, but compilation failure is fatal. Both Python and
Rust still participate in parsing and rendering; native-only means the extension
is required, not that Python orchestration has been rewritten.

The old build-mode and compute-backend environment switches have been removed.
The loader raises on missing, damaged or incompatible extensions, and public
rendering checks the required extension for every valid format/backend route.
The private `_native_points.backend_info()` reports the actual loaded module,
protocol, build profile, point invocation counts, and `rop_calls` / `rop_bytes`.
The public `backend="auto"` /
`backend="replay"` drawing choice remains independent and unchanged.

The native kernel handles ordinary EMF Polyline/Polygon point batches as well as
the supported To records. ROP operations use a separate byte kernel; clipping,
alpha handling, and drawing order remain in the existing renderer. The private
extension protocol is 3, so a source update requires rebuilding an older binary.
WMF object allocation preserves the first free handle using an unused-slot
frontier and a min-heap of recycled handles.

Windows ARM64 installation follows DocVortex's dependency policy: `pyclipper`
may need a source build because no compatible wheel is currently available;
its CI jobs configure the ARM64 MSVC toolchain and test regular dependency
resolution. Intel macOS and musl are not published native targets.

## Benchmarks and fixtures

Native GDI+ Only/Dual fixtures and reference PNGs are included in the source tests
under `tests/fixtures/gdiplus`. They are part of this MIT-licensed project; font
files are not embedded. Regenerate them on Windows with
`./tools/generate_emfplus_fixtures.ps1` or the "Generate GDI+ fixtures" workflow.
Geometric semantics are tested independently of font-specific pixel differences.

For timing and memory measurements, run `python tools/benchmark_rendering.py --replay`.
The same script without `--replay` can benchmark pre-0.3 releases on non-Windows
with identical inputs, explicit 144 DPI and fixed target dimensions. It reports
median conversion time, Python allocation peak, and process RSS high-water mark
where available. CI also uploads Only/Dual review outputs from each platform.

`tools/benchmark_pair.py` compares frozen source trees against a captured real
DOC workload. It runs correctness, paired timing and independent-process RSS
checks separately. Use `--require-native-after` when measuring Rust so a missing,
unused or debug extension cannot pass as a native performance result. The
October 2026 input, batch boundaries and acceptance rules are documented in
[the optimization plan](optimization-plan-2026-10-25.md).

The broader corpus is defined by a pinned upstream source catalog at
`tests/corpus/upstream-metafiles.json` plus actual Office caller captures.
`collect_metafile_corpus.py` preserves aliases and distinguishes actual calls,
embedded-only assets, generated controls and robustness samples.
`benchmark_corpus.py --reference ...` checks real candidate output against a
separate baseline; its timing values alone do not establish a paired speedup.
See [the corpus report](real-corpus-report-2026-10-25.md) for coverage and commands.

`tools/benchmark_corpus_pair.py` compares two source snapshots against that
frozen corpus baseline. It checks the complete input/format matrix, IR, encoded
bytes, fallback pixels, diagnostics and stable rejections before timing. Fresh
processes alternate AB/BA order; whole-corpus warm-up, timed calls and independent
RSS runs are separate. The report keeps primary sources and generated/robustness
controls separate and records per-case samples and actual Release Rust execution.

```bash
python tools/benchmark_corpus_pair.py --corpus /path/to/corpus --reference /path/to/baseline --before-source /path/to/before --after-source /path/to/after --output /path/to/new/paired-results
```

Large replay canvases use bounded source-alpha regions for compositing and
binary-mask coverage regions for opaque solid fills and strokes. The full canvas,
geometry, clipping and resource budgets remain the same. The selected regions
also bound temporary crop storage. See the [third-round plan](optimization-round3-plan-2026-10-25.md)
and [third-round report](optimization-round3-report-2026-10-25.md).

## Publishing

Releases use PyPI Trusted Publishing. Configure the PyPI pending publisher with
project `metafile-render`, owner `myhloli`, repository `metafile-render`, workflow
`publish.yml`, and environment `pypi`. Publishing a GitHub Release such as `v0.4.0`
runs tests, verifies that the tag matches the package version, builds the wheel
matrix, requires exactly the five native release wheels, and uploads them through
OIDC. Pure Python wheels and source distributions are excluded from the release.
