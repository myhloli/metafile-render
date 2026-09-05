# Metafile Render

Render Windows Metafile (WMF) and Enhanced Metafile (EMF) images to SVG, PNG,
JPEG, and WebP on Linux, macOS, and Windows.

Requires Python 3.10–3.14. Runtime dependencies are Pillow and pyclipper;
no Office installation or external conversion executable is required.
Portable replay runs on every platform. Windows optionally uses Pillow’s built-in
GDI renderer first for raster outputs.

## Installation

```bash
pip install metafile-render
```

Python 3.10–3.13 requires Pillow >=11.0.0 and pyclipper >=1.3.0,<2.
Python 3.14 requires Pillow >=12.0.0 and pyclipper >=1.4.0,<2.
Existing compatible dependencies can be retained; these ranges do not pin a fresh installation to older releases.

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

The result contains `data`, `output_format`, `media_type`, `width`, `height`,
`source_format`, `emfplus_mode`, `partial`, and a tuple of `diagnostics`.
Each diagnostic contains a code, level, message, and optional record location.

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

Public exports are `render_metafile`, `MetafileOutputFormat`,
`MetafileBackend`, `MetafileRenderResult`, `MetafileDiagnostic`, `MetafileError`,
`MetafileMalformedError`, `MetafileResourceLimitError`, and `MetafileUnsupportedError`.
The parser, drawing models, and renderer internals are not a stable public API.
Invalid API arguments raise `TypeError` or `ValueError`; malformed, unsupported,
and over-budget images raise the corresponding `MetafileError` subclass.

## Command line

```bash
metafile-render input.emf -o output.svg
metafile-render input.wmf -o output.png --dpi 200 --size 800 600
metafile-render input.emf -o output.webp
python -m metafile_render input.emf -o output.jpg --force
metafile-render input.emf -o transparent.png --backend replay
metafile-render --version
```

The output extension selects the format: `.svg`, `.png`, `.jpg`, `.jpeg`, or `.webp`
(case-insensitive). The output directory must exist. Existing output files are
preserved unless `--force` is supplied; input and output must be different files.
Completed outputs are published atomically. Input reads are bounded.

Exit codes: `0` for a completed conversion (including partial rendering), `1` for
conversion or filesystem errors, and `2` for invalid arguments. Diagnostics go to
stderr. Partial rendering is explicitly reported. Use the Python API to inspect
individual diagnostic fields.

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
record types and source offsets. Only files with no supported drawing operations
raise `MetafileUnsupportedError`. Malformed structures and resource overflows
continue to raise their specific errors. This is not complete GDI+ compatibility.

Font lookup uses installed system fonts and common aliases, trying matching
bold/italic styles before the regular face, then Pillow's default font.
`font_substituted` informational diagnostics identify replacements at their source
record; font substitution alone does not change `partial`. Install the fonts used by the source document for closer text fidelity;
glyph coverage and measurements can vary across systems. Font files are not bundled.
WebP output requires a Pillow build with WebP encoding support, as provided by its
standard wheels; an unavailable encoder raises `MetafileUnsupportedError`.

SVG uses `data-metafile-render="wmf-emf"`, PNG metadata ID
`metafile-render-raster-fallback`, and local clip IDs `metafile-render-clip-N`.
The generated-image marker is not authentication of an arbitrary SVG. Consumers
accepting externally supplied SVG should validate its structure independently.

Fixed budgets bound input bytes, record and object counts, nesting, geometry,
embedded images, and rendering work. Input is limited to 128 MiB; generated SVG
is limited to 64 MiB; the canvas is limited to 8192 per side and 16 million pixels.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/metafile_render
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

On Windows, the virtual environment interpreter is `.venv\Scripts\python.exe`.
CI tests Python 3.10–3.14 on Linux, macOS, and Windows, plus minimum dependency
combinations on Linux and Windows. Windows jobs exercise real GDI rendering.
Core replay tests explicitly select the replay backend. CI also checks static
types and installs both wheel and sdist in environments without MinerU.
Real EMF test images are read from a test-only presentation package dependency.

## Publishing

Releases use PyPI Trusted Publishing. Configure the PyPI pending publisher with
project `metafile-render`, owner `myhloli`, repository `metafile-render`, workflow
`publish.yml`, and environment `pypi`. Publishing a GitHub Release such as `v0.3.0`
runs tests, verifies that the tag matches the package version, builds the wheel
and source distribution, and uploads them through OIDC.

## License

MIT. Copyright (c) 2026 Xiaomeng Zhao (myhloli).

Native GDI+ Only/Dual fixtures and reference PNGs are included in the source tests.
Regenerate them on Windows with `./tools/generate_emfplus_fixtures.ps1` or the
"Generate GDI+ fixtures" workflow. Geometric semantics are tested independently
of font-specific pixel differences.

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
