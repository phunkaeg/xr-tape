// xr-tape - an OpenXR API layer that records what an application submits.
//
// The layer owns nothing game-specific and links no graphics API. Everything it
// records is an OpenXR specification type, which is the entire reason one binary
// serves every mod in the fleet and every mod outside it.
//
// It is an OBSERVER. It forwards every call unchanged and never alters a result.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <openxr/openxr.h>
#include <openxr/openxr_loader_negotiation.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

// ---------------------------------------------------------------- constants

// The trace schema version. ADDITIVE CHANGES ONLY. Seven repositories will read
// these files; a breaking change breaks all of them at once.
constexpr int kSchemaVersion = 1;
constexpr const char* kLayerName = "XR_APILAYER_XRTAPE_recorder";
constexpr const char* kLayerVersion = "0.1.0";

// Graphics-binding struct types, as raw integers on purpose. Matching them by
// value rather than by symbol keeps this translation unit free of d3d11.h,
// d3d12.h, GL and Vulkan - so one build covers every binding and the layer
// cannot fail to compile because a graphics SDK is absent.
// Values copied from openxr.h rather than remembered. The first version of this
// table had OpenGL Win32 at ...3001, which is the XLIB binding, so an OpenGL
// client recorded as "unknown" - caught the first time a real OpenGL client was
// taped.
constexpr int64_t kBindingOpenGLWin32 = 1000023000;      // ..._OPENGL_WIN32_KHR
constexpr int64_t kBindingOpenGLXlib = 1000023001;       // ..._OPENGL_XLIB_KHR
constexpr int64_t kBindingOpenGLXcb = 1000023002;        // ..._OPENGL_XCB_KHR
constexpr int64_t kBindingOpenGLWayland = 1000023003;    // ..._OPENGL_WAYLAND_KHR
constexpr int64_t kBindingOpenGLESAndroid = 1000024001;  // ..._OPENGL_ES_ANDROID_KHR
constexpr int64_t kBindingVulkan = 1000025000;           // ..._VULKAN_KHR (and VULKAN2)
constexpr int64_t kBindingD3D11 = 1000027000;
constexpr int64_t kBindingD3D12 = 1000028000;
constexpr int64_t kBindingMetal = 1000029000;
constexpr int64_t kBindingEglMndx = 1000048004;
// xr-sim's private compatibility bindings. OpenXR defines no Khronos graphics
// binding for D3D9 or D3D10, so these are the values from its own public
// compatibility header.
constexpr int64_t kBindingXrsimD3D9 = 0x58525301;
constexpr int64_t kBindingXrsimD3D10 = 0x58525311;

const char* BindingName(int64_t type) {
    switch (type) {
        case kBindingOpenGLWin32: return "opengl";
        case kBindingOpenGLXlib: return "opengl-xlib";
        case kBindingOpenGLXcb: return "opengl-xcb";
        case kBindingOpenGLWayland: return "opengl-wayland";
        case kBindingOpenGLESAndroid: return "opengles";
        case kBindingVulkan: return "vulkan";
        case kBindingD3D11: return "d3d11";
        case kBindingD3D12: return "d3d12";
        case kBindingMetal: return "metal";
        case kBindingEglMndx: return "egl";
        case kBindingXrsimD3D9: return "d3d9";
        case kBindingXrsimD3D10: return "d3d10";
        default: return nullptr;
    }
}

// ---------------------------------------------------------------- writer

// One writer per process. Buffered stdio; the frame path does a single fwrite of
// a stack buffer and never allocates. The mutex exists because xrWaitFrame,
// xrLocateViews and xrEndFrame are routinely called from different threads.
class TraceWriter {
public:
    bool Open(const std::string& path) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (file_) return true;
        if (fopen_s(&file_, path.c_str(), "wb") != 0 || !file_) {
            file_ = nullptr;
            return false;
        }
        setvbuf(file_, nullptr, _IOFBF, 1 << 16);
        path_ = path;
        return true;
    }

    // Returns false once the frame cap has been reached. The caller must treat
    // that as "stop recording", never as "stop doing the work" - an instrument
    // that changes the behaviour it measures is worse than no instrument.
    bool WriteLine(const char* text, size_t length) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!file_) return false;
        fwrite(text, 1, length, file_);
        fputc('\n', file_);
        ++records_;
        return true;
    }

    void Flush() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (file_) fflush(file_);
    }

    void Close() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (file_) {
            fflush(file_);
            fclose(file_);
            file_ = nullptr;
        }
    }

    bool IsOpen() {
        std::lock_guard<std::mutex> lock(mutex_);
        return file_ != nullptr;
    }

    const std::string& Path() const { return path_; }
    uint64_t Records() const { return records_; }

private:
    std::mutex mutex_;
    FILE* file_ = nullptr;
    std::string path_;
    uint64_t records_ = 0;
};

TraceWriter g_trace;
std::atomic<uint64_t> g_waitSeq{0};
std::atomic<uint64_t> g_recordedFrames{0};
std::atomic<bool> g_truncated{false};
uint64_t g_maxFrames = 20000;   // ~3.7 minutes at 90 Hz; overridable
LARGE_INTEGER g_qpcFreq{};

// ---------------------------------------------------------------- json helpers

void AppendEscaped(std::string& out, const char* text) {
    out.push_back('"');
    if (text) {
        for (const char* p = text; *p; ++p) {
            const unsigned char c = static_cast<unsigned char>(*p);
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if (c < 0x20) {
                        char buf[8];
                        snprintf(buf, sizeof(buf), "\\u%04x", c);
                        out += buf;
                    } else {
                        out.push_back(static_cast<char>(c));
                    }
            }
        }
    }
    out.push_back('"');
}

void AppendFloat(std::string& out, float v) {
    char buf[40];
    // 9 significant digits round-trips a float exactly. A trace that loses
    // precision cannot answer "does the submitted value equal the located one".
    snprintf(buf, sizeof(buf), "%.9g", static_cast<double>(v));
    out += buf;
}

void AppendInt(std::string& out, long long v) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld", v);
    out += buf;
}

void AppendUInt(std::string& out, uint64_t v) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%llu", static_cast<unsigned long long>(v));
    out += buf;
}

// OpenXR handles are POINTERS in a 64-bit process and a uint64_t in a 32-bit
// one, so neither reinterpret_cast nor static_cast compiles for both. Three of
// the fleet's targets are x86, which makes this the difference between a layer
// that serves the whole catalog and one that serves half of it.
//
// Handles are recorded only as identity tokens - compared for equality, never
// interpreted - so a bit-copy is the whole requirement.
template <typename Handle>
uint64_t HandleId(Handle handle) {
    static_assert(sizeof(Handle) <= sizeof(uint64_t), "unexpected OpenXR handle size");
    uint64_t id = 0;
    memcpy(&id, &handle, sizeof(handle));
    return id;
}

void AppendPose(std::string& out, const XrPosef& pose) {
    out += "{\"p\":[";
    AppendFloat(out, pose.position.x); out += ',';
    AppendFloat(out, pose.position.y); out += ',';
    AppendFloat(out, pose.position.z);
    out += "],\"o\":[";
    AppendFloat(out, pose.orientation.x); out += ',';
    AppendFloat(out, pose.orientation.y); out += ',';
    AppendFloat(out, pose.orientation.z); out += ',';
    AppendFloat(out, pose.orientation.w);
    out += "]}";
}

void AppendFov(std::string& out, const XrFovf& fov) {
    // Four independent angles, always. Collapsing them to a scalar or a pair is
    // FAIL-STR-014, and a trace that stored fewer could not detect it.
    out += "{\"l\":";  AppendFloat(out, fov.angleLeft);
    out += ",\"r\":";  AppendFloat(out, fov.angleRight);
    out += ",\"u\":";  AppendFloat(out, fov.angleUp);
    out += ",\"d\":";  AppendFloat(out, fov.angleDown);
    out += "}";
}

int64_t Qpc() {
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return now.QuadPart;
}

void Emit(std::string& line) {
    g_trace.WriteLine(line.c_str(), line.size());
}

// ---------------------------------------------------------------- stamp seam

// The one optional per-project seam. A mod writes key/value text into the file
// named by XRTAPE_STAMP_FILE and the layer folds its contents into the trace
// when it changes. Polled from xrEndFrame and throttled, because reading a file
// on every frame is exactly the kind of instrument cost that shows up as a
// measurement.
//
// KNOWN HAZARD, inherited from FarCry2-vr's command.txt: a writer that rewrites
// this file faster than the poll interval will have writes coalesced. Stamp
// state, not events.
std::string g_stampPath;
std::string g_stampLast;
int64_t g_stampNextPoll = 0;

void PollStamp(int64_t nowQpc) {
    if (g_stampPath.empty()) return;
    if (nowQpc < g_stampNextPoll) return;
    g_stampNextPoll = nowQpc + g_qpcFreq.QuadPart / 4;   // 4 Hz

    FILE* f = nullptr;
    if (fopen_s(&f, g_stampPath.c_str(), "rb") != 0 || !f) return;
    std::string contents;
    char buf[1024];
    size_t got = 0;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) {
        contents.append(buf, got);
        if (contents.size() > 64 * 1024) break;
    }
    fclose(f);
    if (contents == g_stampLast) return;
    g_stampLast = contents;

    std::string line = "{\"r\":\"stamp\",\"text\":";
    AppendEscaped(line, contents.c_str());
    line += "}";
    Emit(line);
}

// ---------------------------------------------------------------- dispatch

struct Dispatch {
    PFN_xrGetInstanceProcAddr GetInstanceProcAddr = nullptr;
    PFN_xrDestroyInstance DestroyInstance = nullptr;
    PFN_xrGetInstanceProperties GetInstanceProperties = nullptr;
    PFN_xrGetSystemProperties GetSystemProperties = nullptr;
    PFN_xrEnumerateViewConfigurationViews EnumerateViewConfigurationViews = nullptr;
    PFN_xrCreateSession CreateSession = nullptr;
    PFN_xrDestroySession DestroySession = nullptr;
    PFN_xrBeginSession BeginSession = nullptr;
    PFN_xrEndSession EndSession = nullptr;
    PFN_xrCreateSwapchain CreateSwapchain = nullptr;
    PFN_xrCreateReferenceSpace CreateReferenceSpace = nullptr;
    PFN_xrLocateViews LocateViews = nullptr;
    PFN_xrWaitFrame WaitFrame = nullptr;
    PFN_xrBeginFrame BeginFrame = nullptr;
    PFN_xrEndFrame EndFrame = nullptr;
    PFN_xrPollEvent PollEvent = nullptr;
};

Dispatch g_next;
XrInstance g_instance = XR_NULL_HANDLE;

template <typename Fn>
void Load(XrInstance instance, const char* name, Fn* target) {
    PFN_xrVoidFunction fn = nullptr;
    if (g_next.GetInstanceProcAddr &&
        XR_SUCCEEDED(g_next.GetInstanceProcAddr(instance, name, &fn))) {
        *target = reinterpret_cast<Fn>(fn);
    }
}

// ---------------------------------------------------------------- hooks

XrResult XRAPI_CALL TapeDestroyInstance(XrInstance instance) {
    if (g_trace.IsOpen()) {
        std::string line = "{\"r\":\"footer\",\"records\":";
        AppendInt(line, static_cast<long long>(g_trace.Records()));
        line += ",\"recordedFrames\":";
        AppendInt(line, static_cast<long long>(g_recordedFrames.load()));
        line += ",\"truncated\":";
        line += g_truncated.load() ? "true" : "false";
        line += ",\"maxFrames\":";
        AppendInt(line, static_cast<long long>(g_maxFrames));
        line += "}";
        Emit(line);
        g_trace.Close();
    }
    return g_next.DestroyInstance ? g_next.DestroyInstance(instance)
                                  : XR_ERROR_FUNCTION_UNSUPPORTED;
}

XrResult XRAPI_CALL TapeGetSystemProperties(XrInstance instance, XrSystemId systemId,
                                            XrSystemProperties* properties) {
    const XrResult result = g_next.GetSystemProperties(instance, systemId, properties);
    if (XR_SUCCEEDED(result) && properties) {
        std::string line = "{\"r\":\"system\",\"systemId\":";
        AppendInt(line, static_cast<long long>(systemId));
        line += ",\"name\":";
        AppendEscaped(line, properties->systemName);
        line += ",\"vendorId\":";
        AppendInt(line, properties->vendorId);
        line += ",\"maxLayerCount\":";
        AppendInt(line, properties->graphicsProperties.maxLayerCount);
        line += ",\"maxSwapchainW\":";
        AppendInt(line, properties->graphicsProperties.maxSwapchainImageWidth);
        line += ",\"maxSwapchainH\":";
        AppendInt(line, properties->graphicsProperties.maxSwapchainImageHeight);
        line += ",\"orientationTracking\":";
        line += properties->trackingProperties.orientationTracking ? "true" : "false";
        line += ",\"positionTracking\":";
        line += properties->trackingProperties.positionTracking ? "true" : "false";
        line += "}";
        Emit(line);
    }
    return result;
}

XrResult XRAPI_CALL TapeEnumerateViewConfigurationViews(
    XrInstance instance, XrSystemId systemId, XrViewConfigurationType viewConfigurationType,
    uint32_t viewCapacityInput, uint32_t* viewCountOutput, XrViewConfigurationView* views) {
    const XrResult result = g_next.EnumerateViewConfigurationViews(
        instance, systemId, viewConfigurationType, viewCapacityInput, viewCountOutput, views);
    if (XR_SUCCEEDED(result) && views && viewCapacityInput > 0 && viewCountOutput) {
        std::string line = "{\"r\":\"viewconfig\",\"type\":";
        AppendInt(line, static_cast<long long>(viewConfigurationType));
        line += ",\"views\":[";
        for (uint32_t i = 0; i < *viewCountOutput && i < viewCapacityInput; ++i) {
            if (i) line += ',';
            line += "{\"recW\":";
            AppendInt(line, views[i].recommendedImageRectWidth);
            line += ",\"recH\":";
            AppendInt(line, views[i].recommendedImageRectHeight);
            line += ",\"maxW\":";
            AppendInt(line, views[i].maxImageRectWidth);
            line += ",\"maxH\":";
            AppendInt(line, views[i].maxImageRectHeight);
            line += ",\"recSamples\":";
            AppendInt(line, views[i].recommendedSwapchainSampleCount);
            line += "}";
        }
        line += "]}";
        Emit(line);
    }
    return result;
}

XrResult XRAPI_CALL TapeCreateSession(XrInstance instance, const XrSessionCreateInfo* createInfo,
                                      XrSession* session) {
    // Identify the graphics binding by walking the next chain. This is the only
    // place the layer learns which renderer the application presents to OpenXR,
    // and it is a property of the CALL, not of the game.
    const char* binding = nullptr;
    int64_t bindingType = 0;
    if (createInfo) {
        const XrBaseInStructure* node =
            reinterpret_cast<const XrBaseInStructure*>(createInfo->next);
        while (node) {
            const char* name = BindingName(static_cast<int64_t>(node->type));
            if (name) {
                binding = name;
                bindingType = static_cast<int64_t>(node->type);
                break;
            }
            // Remember the first unrecognised chained struct so an unknown or
            // future binding is still diagnosable. Recording "unknown" and
            // discarding the number would throw away the one fact that
            // identifies it.
            if (bindingType == 0) bindingType = static_cast<int64_t>(node->type);
            node = node->next;
        }
    }

    const XrResult result = g_next.CreateSession(instance, createInfo, session);

    std::string line = "{\"r\":\"session\",\"binding\":";
    AppendEscaped(line, binding ? binding : "unknown");
    line += ",\"bindingType\":";
    AppendInt(line, bindingType);
    line += ",\"systemId\":";
    AppendInt(line, createInfo ? static_cast<long long>(createInfo->systemId) : 0);
    line += ",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);
    return result;
}

XrResult XRAPI_CALL TapeCreateSwapchain(XrSession session, const XrSwapchainCreateInfo* createInfo,
                                        XrSwapchain* swapchain) {
    const XrResult result = g_next.CreateSwapchain(session, createInfo, swapchain);
    if (createInfo) {
        std::string line = "{\"r\":\"swapchain\",\"handle\":";
        AppendUInt(line, (XR_SUCCEEDED(result) && swapchain) ? HandleId(*swapchain) : 0);
        line += ",\"format\":";
        AppendInt(line, createInfo->format);
        line += ",\"w\":";
        AppendInt(line, createInfo->width);
        line += ",\"h\":";
        AppendInt(line, createInfo->height);
        line += ",\"arraySize\":";
        AppendInt(line, createInfo->arraySize);
        line += ",\"faceCount\":";
        AppendInt(line, createInfo->faceCount);
        line += ",\"mipCount\":";
        AppendInt(line, createInfo->mipCount);
        line += ",\"sampleCount\":";
        AppendInt(line, createInfo->sampleCount);
        line += ",\"usage\":";
        AppendInt(line, static_cast<long long>(createInfo->usageFlags));
        line += ",\"result\":";
        AppendInt(line, static_cast<long long>(result));
        line += "}";
        Emit(line);
    }
    return result;
}

XrResult XRAPI_CALL TapeCreateReferenceSpace(XrSession session,
                                             const XrReferenceSpaceCreateInfo* createInfo,
                                             XrSpace* space) {
    const XrResult result = g_next.CreateReferenceSpace(session, createInfo, space);
    if (createInfo) {
        std::string line = "{\"r\":\"space\",\"handle\":";
        AppendUInt(line, (XR_SUCCEEDED(result) && space) ? HandleId(*space) : 0);
        line += ",\"spaceType\":";
        AppendInt(line, static_cast<long long>(createInfo->referenceSpaceType));
        line += ",\"pose\":";
        AppendPose(line, createInfo->poseInReferenceSpace);
        line += ",\"result\":";
        AppendInt(line, static_cast<long long>(result));
        line += "}";
        Emit(line);
    }
    return result;
}

XrResult XRAPI_CALL TapeBeginSession(XrSession session, const XrSessionBeginInfo* beginInfo) {
    const XrResult result = g_next.BeginSession(session, beginInfo);
    std::string line = "{\"r\":\"beginsession\",\"viewConfig\":";
    AppendInt(line, beginInfo ? static_cast<long long>(beginInfo->primaryViewConfigurationType) : 0);
    line += ",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);
    return result;
}

XrResult XRAPI_CALL TapeEndSession(XrSession session) {
    const XrResult result = g_next.EndSession(session);
    std::string line = "{\"r\":\"endsession\",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);
    return result;
}

XrResult XRAPI_CALL TapePollEvent(XrInstance instance, XrEventDataBuffer* eventData) {
    const XrResult result = g_next.PollEvent(instance, eventData);
    if (result == XR_SUCCESS && eventData) {
        if (eventData->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
            const auto* ev = reinterpret_cast<const XrEventDataSessionStateChanged*>(eventData);
            std::string line = "{\"r\":\"sessionstate\",\"state\":";
            AppendInt(line, static_cast<long long>(ev->state));
            line += ",\"time\":";
            AppendInt(line, static_cast<long long>(ev->time));
            line += ",\"qpc\":";
            AppendInt(line, Qpc());
            line += "}";
            Emit(line);
        } else if (eventData->type == XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING) {
            std::string line = "{\"r\":\"instanceloss\",\"qpc\":";
            AppendInt(line, Qpc());
            line += "}";
            Emit(line);
        }
    }
    return result;
}

XrResult XRAPI_CALL TapeWaitFrame(XrSession session, const XrFrameWaitInfo* frameWaitInfo,
                                  XrFrameState* frameState) {
    const int64_t before = Qpc();
    const XrResult result = g_next.WaitFrame(session, frameWaitInfo, frameState);
    const int64_t after = Qpc();
    const uint64_t seq = g_waitSeq.fetch_add(1) + 1;

    if (g_recordedFrames.load() >= g_maxFrames) {
        g_truncated.store(true);
        return result;
    }

    std::string line = "{\"r\":\"wait\",\"seq\":";
    AppendInt(line, static_cast<long long>(seq));
    line += ",\"displayTime\":";
    AppendInt(line, frameState ? static_cast<long long>(frameState->predictedDisplayTime) : 0);
    line += ",\"displayPeriod\":";
    AppendInt(line, frameState ? static_cast<long long>(frameState->predictedDisplayPeriod) : 0);
    line += ",\"shouldRender\":";
    line += (frameState && frameState->shouldRender) ? "true" : "false";
    line += ",\"qpcIn\":";
    AppendInt(line, before);
    line += ",\"qpcOut\":";
    AppendInt(line, after);
    line += ",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);
    return result;
}

XrResult XRAPI_CALL TapeBeginFrame(XrSession session, const XrFrameBeginInfo* frameBeginInfo) {
    const int64_t before = Qpc();
    const XrResult result = g_next.BeginFrame(session, frameBeginInfo);
    if (g_recordedFrames.load() >= g_maxFrames) return result;

    std::string line = "{\"r\":\"begin\",\"seq\":";
    AppendInt(line, static_cast<long long>(g_waitSeq.load()));
    line += ",\"qpc\":";
    AppendInt(line, before);
    line += ",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);
    return result;
}

XrResult XRAPI_CALL TapeLocateViews(XrSession session, const XrViewLocateInfo* viewLocateInfo,
                                    XrViewState* viewState, uint32_t viewCapacityInput,
                                    uint32_t* viewCountOutput, XrView* views) {
    const XrResult result = g_next.LocateViews(session, viewLocateInfo, viewState,
                                               viewCapacityInput, viewCountOutput, views);
    if (!XR_SUCCEEDED(result) || !views || viewCapacityInput == 0 || !viewCountOutput) {
        return result;
    }
    if (g_recordedFrames.load() >= g_maxFrames) return result;

    const uint32_t count =
        (*viewCountOutput < viewCapacityInput) ? *viewCountOutput : viewCapacityInput;

    std::string line = "{\"r\":\"views\",\"seq\":";
    AppendInt(line, static_cast<long long>(g_waitSeq.load()));
    line += ",\"displayTime\":";
    AppendInt(line, viewLocateInfo ? static_cast<long long>(viewLocateInfo->displayTime) : 0);
    line += ",\"viewConfig\":";
    AppendInt(line,
              viewLocateInfo ? static_cast<long long>(viewLocateInfo->viewConfigurationType) : 0);
    line += ",\"space\":";
    AppendUInt(line, viewLocateInfo ? HandleId(viewLocateInfo->space) : 0);
    line += ",\"stateFlags\":";
    AppendInt(line, viewState ? static_cast<long long>(viewState->viewStateFlags) : 0);
    line += ",\"views\":[";
    for (uint32_t i = 0; i < count; ++i) {
        if (i) line += ',';
        line += "{\"pose\":";
        AppendPose(line, views[i].pose);
        line += ",\"fov\":";
        AppendFov(line, views[i].fov);
        line += "}";
    }
    line += "],\"qpc\":";
    AppendInt(line, Qpc());
    line += "}";
    Emit(line);
    return result;
}

void AppendProjectionLayer(std::string& out, const XrCompositionLayerProjection* proj) {
    out += "{\"type\":\"projection\",\"flags\":";
    AppendInt(out, static_cast<long long>(proj->layerFlags));
    out += ",\"space\":";
    AppendUInt(out, HandleId(proj->space));
    out += ",\"views\":[";
    for (uint32_t v = 0; v < proj->viewCount; ++v) {
        if (v) out += ',';
        const XrCompositionLayerProjectionView& view = proj->views[v];
        out += "{\"pose\":";
        AppendPose(out, view.pose);
        out += ",\"fov\":";
        AppendFov(out, view.fov);
        out += ",\"sub\":{\"swapchain\":";
        AppendUInt(out, HandleId(view.subImage.swapchain));
        out += ",\"arrayIndex\":";
        AppendInt(out, view.subImage.imageArrayIndex);
        out += ",\"rect\":[";
        AppendInt(out, view.subImage.imageRect.offset.x); out += ',';
        AppendInt(out, view.subImage.imageRect.offset.y); out += ',';
        AppendInt(out, view.subImage.imageRect.extent.width); out += ',';
        AppendInt(out, view.subImage.imageRect.extent.height);
        out += "]}";

        // Depth is in the schema at v1 deliberately. SOMAVR submits
        // XrCompositionLayerDepthInfoKHR, and the runtime it was tested against
        // had no depth handling at all - so that path could be neither observed
        // nor failed. Reserving the slot now costs nothing; adding it later
        // would be a breaking change for every other consumer.
        const XrBaseInStructure* node =
            reinterpret_cast<const XrBaseInStructure*>(view.next);
        while (node) {
            if (node->type == XR_TYPE_COMPOSITION_LAYER_DEPTH_INFO_KHR) {
                const auto* depth =
                    reinterpret_cast<const XrCompositionLayerDepthInfoKHR*>(node);
                out += ",\"depth\":{\"minDepth\":";
                AppendFloat(out, depth->minDepth);
                out += ",\"maxDepth\":";
                AppendFloat(out, depth->maxDepth);
                out += ",\"nearZ\":";
                AppendFloat(out, depth->nearZ);
                out += ",\"farZ\":";
                AppendFloat(out, depth->farZ);
                out += ",\"swapchain\":";
                AppendUInt(out, HandleId(depth->subImage.swapchain));
                out += ",\"arrayIndex\":";
                AppendInt(out, depth->subImage.imageArrayIndex);
                out += "}";
                break;
            }
            node = node->next;
        }
        out += "}";
    }
    out += "]}";
}

XrResult XRAPI_CALL TapeEndFrame(XrSession session, const XrFrameEndInfo* frameEndInfo) {
    const int64_t qpc = Qpc();
    const XrResult result = g_next.EndFrame(session, frameEndInfo);

    const uint64_t recorded = g_recordedFrames.load();
    if (recorded >= g_maxFrames) {
        g_truncated.store(true);
        return result;
    }
    g_recordedFrames.store(recorded + 1);

    std::string line = "{\"r\":\"end\",\"seq\":";
    AppendInt(line, static_cast<long long>(g_waitSeq.load()));
    line += ",\"displayTime\":";
    AppendInt(line, frameEndInfo ? static_cast<long long>(frameEndInfo->displayTime) : 0);
    line += ",\"blendMode\":";
    AppendInt(line,
              frameEndInfo ? static_cast<long long>(frameEndInfo->environmentBlendMode) : 0);
    line += ",\"layerCount\":";
    AppendInt(line, frameEndInfo ? frameEndInfo->layerCount : 0);
    line += ",\"layers\":[";
    if (frameEndInfo && frameEndInfo->layers) {
        for (uint32_t i = 0; i < frameEndInfo->layerCount; ++i) {
            if (i) line += ',';
            const XrCompositionLayerBaseHeader* header = frameEndInfo->layers[i];
            if (!header) {
                line += "{\"type\":\"null\"}";
                continue;
            }
            if (header->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
                AppendProjectionLayer(
                    line, reinterpret_cast<const XrCompositionLayerProjection*>(header));
            } else {
                line += "{\"type\":\"other\",\"structType\":";
                AppendInt(line, static_cast<long long>(header->type));
                line += ",\"flags\":";
                AppendInt(line, static_cast<long long>(header->layerFlags));
                line += "}";
            }
        }
    }
    line += "],\"qpc\":";
    AppendInt(line, qpc);
    line += ",\"result\":";
    AppendInt(line, static_cast<long long>(result));
    line += "}";
    Emit(line);

    PollStamp(qpc);
    if ((recorded % 256) == 0) g_trace.Flush();
    return result;
}

// ---------------------------------------------------------------- proc addr

XrResult XRAPI_CALL TapeGetInstanceProcAddr(XrInstance instance, const char* name,
                                            PFN_xrVoidFunction* function) {
#define XRTAPE_INTERCEPT(fnName, impl)                                     \
    if (strcmp(name, fnName) == 0) {                                       \
        *function = reinterpret_cast<PFN_xrVoidFunction>(impl);            \
        return XR_SUCCESS;                                                 \
    }

    if (name && function) {
        XRTAPE_INTERCEPT("xrDestroyInstance", TapeDestroyInstance)
        XRTAPE_INTERCEPT("xrGetSystemProperties", TapeGetSystemProperties)
        XRTAPE_INTERCEPT("xrEnumerateViewConfigurationViews",
                         TapeEnumerateViewConfigurationViews)
        XRTAPE_INTERCEPT("xrCreateSession", TapeCreateSession)
        XRTAPE_INTERCEPT("xrCreateSwapchain", TapeCreateSwapchain)
        XRTAPE_INTERCEPT("xrCreateReferenceSpace", TapeCreateReferenceSpace)
        XRTAPE_INTERCEPT("xrBeginSession", TapeBeginSession)
        XRTAPE_INTERCEPT("xrEndSession", TapeEndSession)
        XRTAPE_INTERCEPT("xrPollEvent", TapePollEvent)
        XRTAPE_INTERCEPT("xrWaitFrame", TapeWaitFrame)
        XRTAPE_INTERCEPT("xrBeginFrame", TapeBeginFrame)
        XRTAPE_INTERCEPT("xrLocateViews", TapeLocateViews)
        XRTAPE_INTERCEPT("xrEndFrame", TapeEndFrame)
    }
#undef XRTAPE_INTERCEPT

    return g_next.GetInstanceProcAddr ? g_next.GetInstanceProcAddr(instance, name, function)
                                      : XR_ERROR_FUNCTION_UNSUPPORTED;
}

// ---------------------------------------------------------------- setup

std::string EnvOrEmpty(const char* name) {
    char* value = nullptr;
    size_t length = 0;
    if (_dupenv_s(&value, &length, name) != 0 || !value) return std::string();
    std::string result(value);
    free(value);
    return result;
}

void MakeDirectories(const std::string& path) {
    std::string current;
    for (size_t i = 0; i < path.size(); ++i) {
        current.push_back(path[i]);
        if (path[i] == '\\' || path[i] == '/') CreateDirectoryA(current.c_str(), nullptr);
    }
    CreateDirectoryA(path.c_str(), nullptr);
}

std::string TraceDirectory() {
    // Per-project directories from the first line, not after the first
    // collision. Several projects already share %LOCALAPPDATA% roots on this
    // machine and read each other's state.
    std::string dir = EnvOrEmpty("XRTAPE_DIR");
    if (!dir.empty()) return dir;
    std::string local = EnvOrEmpty("LOCALAPPDATA");
    if (local.empty()) local = ".";
    return local + "\\xr-tape\\default";
}

void OpenTrace(const XrInstanceCreateInfo* createInfo) {
    QueryPerformanceFrequency(&g_qpcFreq);

    const std::string maxFrames = EnvOrEmpty("XRTAPE_MAX_FRAMES");
    if (!maxFrames.empty()) {
        const long long parsed = _atoi64(maxFrames.c_str());
        if (parsed > 0) g_maxFrames = static_cast<uint64_t>(parsed);
    }
    g_stampPath = EnvOrEmpty("XRTAPE_STAMP_FILE");

    const std::string dir = TraceDirectory();
    MakeDirectories(dir);

    char exePath[MAX_PATH] = {0};
    GetModuleFileNameA(nullptr, exePath, MAX_PATH);
    const char* exeName = strrchr(exePath, '\\');
    exeName = exeName ? exeName + 1 : exePath;

    SYSTEMTIME st;
    GetSystemTime(&st);
    char stamp[64];
    snprintf(stamp, sizeof(stamp), "%04u%02u%02u-%02u%02u%02u", st.wYear, st.wMonth, st.wDay,
             st.wHour, st.wMinute, st.wSecond);

    char file[MAX_PATH * 2];
    snprintf(file, sizeof(file), "%s\\trace-%s-%lu-%s.ndjson", dir.c_str(), exeName,
             GetCurrentProcessId(), stamp);
    if (!g_trace.Open(file)) return;

    std::string line = "{\"r\":\"header\",\"schema\":";
    AppendInt(line, kSchemaVersion);
    line += ",\"layer\":";
    AppendEscaped(line, kLayerName);
    line += ",\"layerVersion\":";
    AppendEscaped(line, kLayerVersion);
    line += ",\"exe\":";
    AppendEscaped(line, exePath);
    line += ",\"pid\":";
    AppendInt(line, GetCurrentProcessId());
    line += ",\"utc\":";
    AppendEscaped(line, stamp);
    line += ",\"qpcFreq\":";
    AppendInt(line, g_qpcFreq.QuadPart);
    line += ",\"maxFrames\":";
    AppendInt(line, static_cast<long long>(g_maxFrames));
    line += ",\"pointerBits\":";
    AppendInt(line, static_cast<long long>(sizeof(void*) * 8));
    line += "}";
    Emit(line);

    if (createInfo) {
        std::string app = "{\"r\":\"instance\",\"appName\":";
        AppendEscaped(app, createInfo->applicationInfo.applicationName);
        app += ",\"appVersion\":";
        AppendInt(app, createInfo->applicationInfo.applicationVersion);
        app += ",\"engineName\":";
        AppendEscaped(app, createInfo->applicationInfo.engineName);
        app += ",\"apiVersion\":";
        AppendInt(app, static_cast<long long>(createInfo->applicationInfo.apiVersion));
        app += ",\"extensions\":[";
        for (uint32_t i = 0; i < createInfo->enabledExtensionCount; ++i) {
            if (i) app += ',';
            AppendEscaped(app, createInfo->enabledExtensionNames[i]);
        }
        app += "]}";
        Emit(app);
    }
}

void RecordRuntimeIdentity(XrInstance instance) {
    // Read the runtime's own name rather than trusting what was requested.
    // Every launcher in this fleet asserts this string because a run that
    // silently used the wrong runtime looks exactly like one that used the
    // right runtime. The trace must carry the answer, not the intent.
    if (!g_next.GetInstanceProperties) return;
    XrInstanceProperties props{XR_TYPE_INSTANCE_PROPERTIES};
    if (!XR_SUCCEEDED(g_next.GetInstanceProperties(instance, &props))) return;

    std::string line = "{\"r\":\"runtime\",\"name\":";
    AppendEscaped(line, props.runtimeName);
    line += ",\"version\":";
    AppendInt(line, static_cast<long long>(props.runtimeVersion));
    line += "}";
    Emit(line);
}

XrResult XRAPI_CALL TapeCreateApiLayerInstance(const XrInstanceCreateInfo* info,
                                               const XrApiLayerCreateInfo* apiLayerInfo,
                                               XrInstance* instance) {
    if (!apiLayerInfo || !apiLayerInfo->nextInfo) return XR_ERROR_INITIALIZATION_FAILED;

    XrApiLayerCreateInfo nextLayerInfo = *apiLayerInfo;
    nextLayerInfo.nextInfo = apiLayerInfo->nextInfo->next;

    const XrResult result =
        apiLayerInfo->nextInfo->nextCreateApiLayerInstance(info, &nextLayerInfo, instance);
    if (!XR_SUCCEEDED(result)) return result;

    g_instance = *instance;
    g_next.GetInstanceProcAddr = apiLayerInfo->nextInfo->nextGetInstanceProcAddr;

    Load(*instance, "xrDestroyInstance", &g_next.DestroyInstance);
    Load(*instance, "xrGetInstanceProperties", &g_next.GetInstanceProperties);
    Load(*instance, "xrGetSystemProperties", &g_next.GetSystemProperties);
    Load(*instance, "xrEnumerateViewConfigurationViews",
         &g_next.EnumerateViewConfigurationViews);
    Load(*instance, "xrCreateSession", &g_next.CreateSession);
    Load(*instance, "xrDestroySession", &g_next.DestroySession);
    Load(*instance, "xrBeginSession", &g_next.BeginSession);
    Load(*instance, "xrEndSession", &g_next.EndSession);
    Load(*instance, "xrCreateSwapchain", &g_next.CreateSwapchain);
    Load(*instance, "xrCreateReferenceSpace", &g_next.CreateReferenceSpace);
    Load(*instance, "xrLocateViews", &g_next.LocateViews);
    Load(*instance, "xrWaitFrame", &g_next.WaitFrame);
    Load(*instance, "xrBeginFrame", &g_next.BeginFrame);
    Load(*instance, "xrEndFrame", &g_next.EndFrame);
    Load(*instance, "xrPollEvent", &g_next.PollEvent);

    OpenTrace(info);
    RecordRuntimeIdentity(*instance);
    return result;
}

}  // namespace

extern "C" __declspec(dllexport) XrResult XRAPI_CALL
xrNegotiateLoaderApiLayerInterface(const XrNegotiateLoaderInfo* loaderInfo,
                                   const char* apiLayerName,
                                   XrNegotiateApiLayerRequest* apiLayerRequest) {
    (void)apiLayerName;
    if (!loaderInfo || !apiLayerRequest) return XR_ERROR_INITIALIZATION_FAILED;
    if (loaderInfo->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO ||
        apiLayerRequest->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST) {
        return XR_ERROR_INITIALIZATION_FAILED;
    }
    if (loaderInfo->minInterfaceVersion > XR_CURRENT_LOADER_API_LAYER_VERSION ||
        loaderInfo->maxInterfaceVersion < XR_CURRENT_LOADER_API_LAYER_VERSION) {
        return XR_ERROR_INITIALIZATION_FAILED;
    }

    apiLayerRequest->layerInterfaceVersion = XR_CURRENT_LOADER_API_LAYER_VERSION;
    apiLayerRequest->layerApiVersion = XR_CURRENT_API_VERSION;
    apiLayerRequest->getInstanceProcAddr = TapeGetInstanceProcAddr;
    apiLayerRequest->createApiLayerInstance = TapeCreateApiLayerInstance;
    return XR_SUCCESS;
}
