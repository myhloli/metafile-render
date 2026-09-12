---
name: metafile-render
description: Convert WMF and EMF files to SVG, PNG, JPEG, or WebP using metafile-render, or integrate its public Python API and interpret rendering diagnostics. Use for Windows metafile conversion, including images already extracted from Office documents; not for generic image conversion or maintaining the renderer itself.
---

# Metafile Render

Convert existing WMF/EMF files and report both the output and any rendering
limitations. Use the CLI for file conversions and the public Python API when
integrating into an application or inspecting structured diagnostics.

## Prepare

Use an existing suitable Python environment where possible. The package supports
Python 3.10–3.14 on Linux, macOS, and Windows. Install it into the selected
environment if needed:

```bash
python -m pip install metafile-render
python -m metafile_render --version
```

Pillow and pyclipper are installed as dependencies. Office and external conversion
programs are not required. Use the selected interpreter for all commands;
`python -m metafile_render` avoids depending on a console script from another
environment. Examples below assume an existing `input.emf`; WMF works the same way.

Identify the source file and requested destination. Create the destination parent
directory if needed. Preserve existing files unless replacement is requested;
input and output must refer to different files, including symlinks or hardlinks.

## Choose output and parameters

Follow the requested format or destination extension. If neither is specified,
use PNG. Choose a new output name when the default destination already exists.

| Format | Use and behavior |
| --- | --- |
| PNG | Default raster output; preserves transparency with `replay`. |
| SVG | Browser-friendly, self-contained output with embedded images; some operations become raster content, so it is not necessarily fully vector. |
| JPEG | Opaque raster output with a white background; API name is `jpeg`, while CLI accepts `.jpg` and `.jpeg`. |
| WebP | Lossy raster output that preserves transparency with `replay`; requires a Pillow build with WebP encoding support. |

- Omitted DPI means **200 for every format, including SVG**. Explicit DPI must be
  an integer from 1 through 1200.
- `--size WIDTH HEIGHT` / `size_hint=(width, height)` supplies positive integer
  pixel dimensions. It takes precedence over file/DPI-derived dimensions and is
  useful for WMF files without physical dimensions. Canvas limits can reduce the
  actual dimensions; report the result rather than assuming the requested size.
- Keep `backend="auto"` unless the task needs portable replay or transparency in
  PNG/WebP, in which case select `replay`. SVG always uses replay. With `auto`,
  eligible Windows raster conversions prefer native GDI; other platforms use
  replay. Native GDI outputs an opaque white background. Replay preserves source
  transparency; it does not remove white content painted by the source.

## Convert with the CLI

```bash
python -m metafile_render input.emf -o output.png
python -m metafile_render input.emf -o output.svg
python -m metafile_render input.emf -o transparent.png --backend replay
python -m metafile_render input.emf -o sized.webp --size 800 600 --backend replay
python -m metafile_render input.emf -o output.jpg --dpi 300
```

The destination extension selects the format, case-insensitively. The parent
directory must exist. Add `--force` only when replacing an existing output is
intended; it does not permit overwriting the input file. The CLI publishes
completed files atomically.

Capture stderr as well as the exit code:

- `0`: conversion completed, **including partial rendering**. Inspect stderr for
  `partial rendering` and diagnostic messages before describing the result.
- `1`: conversion or filesystem failure. Report the error; do not present an
  existing destination left over from an earlier run as a new result.
- `2`: invalid arguments. Correct the arguments using the constraints above.

For multiple files, invoke the CLI per file or iterate over the public API;
retain each file's outcome and diagnostics. There is no dedicated batch flag.

## Use the Python API

`render_metafile` accepts file **bytes**, not a path, and returns encoded bytes
and metadata. Use public exports from `metafile_render` rather than parser or
backend internals. This example uses exclusive output creation to avoid silently
replacing an existing file:

```python
from pathlib import Path
import sys

from metafile_render import MetafileError, render_metafile

source = Path("input.emf")
destination = Path("output.svg")

try:
    result = render_metafile(
        source.read_bytes(),
        output_format="svg",
        dpi=None,
        size_hint=None,
        backend="auto",
    )
    with destination.open("xb") as stream:
        stream.write(result.data)
except MetafileError as error:
    print(f"{error.code}: {error}", file=sys.stderr)
    raise SystemExit(1)
except OSError as error:
    print(f"File operation failed: {error}", file=sys.stderr)
    raise SystemExit(1)

print(destination.resolve(), result.width, result.height, result.media_type)
print(f"source={result.source_format}, emfplus={result.emfplus_mode}")
if result.partial:
    print("Partial rendering.", file=sys.stderr)
for item in result.diagnostics:
    print(f"{item.level} [{item.code}] {item.message}", file=sys.stderr)
```

For structured reporting, diagnostics also expose optional `record_type`,
`record_index`, and byte `offset` fields. API argument errors raise `TypeError`
or `ValueError`; fix the call rather than treating them as input corruption.
File operations can independently raise `OSError`.

## Interpret quality and failures

`partial=True` means content was approximated or skipped. Deliver the usable
output with the relevant limitations, and preserve diagnostic codes when further
investigation is needed. `font_substituted` and `native_backend_fallback` alone
do not mark a result as partial. Fonts come from the system; installing the source
document's fonts may improve text fidelity. Even `partial=False` does not certify
pixel-identical reproduction.

Common EMF+ Only content is supported; Dual files use their EMF fallback stream.
Full GDI+ fidelity is not guaranteed. Do not infer that a partial result can be
fixed simply by changing DPI or output format.

| Public exception / code | Response |
| --- | --- |
| `MetafileMalformedError` / `malformed` | Report invalid structure; obtain an intact source or re-export it. |
| `MetafileUnsupportedError` / `unsupported` | Report the unsupported input feature or unavailable encoder. If WebP encoding is unavailable, choose another format only when the task allows it. |
| `MetafileResourceLimitError` / `resource_limit` | Report the exceeded budget. Lower DPI/size may help rendering-work limits; input and structural limits need a smaller or simpler source. Do not disable limits or repeatedly retry unchanged input. |

All three inherit from `MetafileError`. Fixed limits include 128 MiB of input,
64 MiB of generated SVG, and a canvas of at most 8192 pixels per side and
16 million pixels total. Other geometry and rendering-work budgets also apply.

## Check and deliver

Confirm that the new output exists and is readable. Inspect raster dimensions
with Pillow or SVG dimensions from its root element; use the API's `width` and
`height` when available. For appearance-sensitive requests, open or render the
output and check visible text, clipping, and transparency. An SVG preview may
require a browser; successful conversion alone is not a visual fidelity check.

Return the output path or link, actual pixel dimensions, format, and any partial
rendering or relevant font/backend diagnostics. Distinguish successful output,
partial output, and failure. Report which visual checks were actually performed.
