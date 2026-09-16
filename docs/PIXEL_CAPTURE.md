# Stereo pixel capture extension

Stereo image comparison is feasible in xr-tape, but it cannot share the v1
recorder's graphics-API-neutral implementation. OpenXR projection layers carry
opaque swapchain handles and subimage coordinates; the native textures appear
only through the renderer-specific structures returned by
`xrEnumerateSwapchainImages`.

The safe design is an opt-in family of capture backends behind one trace
contract. A run without a backend continues to record the existing v1 wire
trace and reports pixel checks as `SKIP`.

## Capture point

Capture the application-owned swapchain image after rendering and before the
runtime/compositor consumes or reuses it. For each graphics API the layer must
intercept:

1. `xrCreateSwapchain` to retain format, dimensions, array size, and usage.
2. `xrEnumerateSwapchainImages` to associate the opaque `XrSwapchain` with its
   native images.
3. `xrAcquireSwapchainImage` to record the acquired image index.
4. `xrReleaseSwapchainImage` to copy the completed native image into a staging
   resource before ownership returns to the runtime.
5. `xrEndFrame` to associate the staged image with each submitted eye's
   `imageArrayIndex` and `imageRect`.

Copying a post-compositor mirror or headset screenshot is not equivalent. It
reintroduces the mono blind spot this extension is intended to close.

## Staged backend plan

### 1. D3D11 reference backend

Use the `ID3D11Device` from `XrGraphicsBindingD3D11KHR`, retain the
`ID3D11Texture2D` values returned for each swapchain image, and copy the released
subresources to staging textures on the same device. Preserve array slice,
format, submitted rectangle, and color-space metadata. Readback must use an
explicit completion query and a bounded timeout; no instrumentation wait may
hang the application.

Start synchronously and sparsely—for example one requested frame—so correctness
can be falsified before adding a background encoder. A later worker may encode
completed staging resources after the runtime call, but it must not call an
unprotected application immediate context from another thread.

### 2. D3D12 and Vulkan

These require explicit resource-state/layout transitions, command submission,
and fences on the application's queue. They must be separate backends with
their own fault tests; a D3D11 success is not evidence that either is safe.

### 3. OpenGL

Readback must occur while the application-provided context is current on the
calling thread. Pixel-buffer objects can reduce stalls, but context ownership
prevents treating this as the D3D11 worker with different function names.

### 4. D3D9/D3D10 and renderer bridges

xr-tape sees the OpenXR-facing binding, not necessarily the game's native
renderer. Dishonored and SWAT 4, for example, may present a D3D12 bridge even
though the games render through D3D9. Capture the resource submitted to OpenXR;
use a game-side or bridge-side capture when the question concerns pixels before
that transport.

## Trace and artifacts

Keep large pixels outside NDJSON. Add an additive `image` record containing:

- frame/wait sequence and display time;
- eye/view index;
- swapchain identity, acquired image index, array slice, and submitted rect;
- graphics API, native format, color space, width, height, and row pitch;
- artifact-relative path plus a content hash;
- capture status and an explicit failure/timeout reason.

Offline comparison should operate on linearized images and publish at least:
mean absolute difference, changed-pixel fraction, structural similarity, black
fraction, and a temporal hash/history. Thresholds remain scene- and
effect-dependent; the trace stores measurements, not a universal notion of
"correct stereo."

## Required falsification matrix

Before a backend can make a pixel check pass, demonstrate that it fails each
relevant control:

- identical pixels in two distinct swapchain subimages;
- correct distinct pixels with different row pitch or array slices;
- one eye black;
- one eye one frame stale;
- submitted subrect smaller than the native texture;
- sRGB versus linear resources;
- multisampled or unsupported formats producing `SKIP`, never fabricated data;
- GPU completion timeout without hanging or changing the application's result;
- capture disabled producing byte-for-byte-equivalent v1 wire behavior.

The application-owned per-eye rate counter remains leg 0 even after this
extension exists. Pixel capture can prove image content; it cannot prove that a
particular engine replay/camera path executed unless that fact is stamped by
the producer.
