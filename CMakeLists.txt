cmake_minimum_required(VERSION 3.24)
project(xr-tape LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT WIN32)
    message(FATAL_ERROR "xr-tape targets Windows.")
endif()

# The OpenXR headers are the layer's ONLY dependency. It links no loader and no
# graphics API: the loader calls us, and we call onward through function
# pointers. One binary therefore covers D3D9/10/11/12, OpenGL and Vulkan, and it
# cannot fail to build because a graphics SDK is missing.
set(XRTAPE_OPENXR_INCLUDE "" CACHE PATH "Directory containing openxr/openxr.h")

if(NOT XRTAPE_OPENXR_INCLUDE)
    foreach(candidate
            "${CMAKE_CURRENT_SOURCE_DIR}/third_party/openxr/include"
            "${CMAKE_CURRENT_SOURCE_DIR}/../xr-sim/third_party/openxr/include"
            "${CMAKE_CURRENT_SOURCE_DIR}/../Xr-sim/third_party/openxr/include")
        if(EXISTS "${candidate}/openxr/openxr.h")
            set(XRTAPE_OPENXR_INCLUDE "${candidate}")
            break()
        endif()
    endforeach()
endif()

if(NOT EXISTS "${XRTAPE_OPENXR_INCLUDE}/openxr/openxr.h")
    message(FATAL_ERROR
        "OpenXR headers not found. Pass -DXRTAPE_OPENXR_INCLUDE=<dir containing openxr/openxr.h>.")
endif()
message(STATUS "xr-tape OpenXR headers: ${XRTAPE_OPENXR_INCLUDE}")

# The .def publishes xrNegotiateLoaderApiLayerInterface UNDECORATED. Required on x86, where
# XRAPI_CALL's __stdcall would otherwise export _name@12 and the loader would report the
# layer as not present. Harmless on x64. See layer/xrtape_layer.def.
add_library(xrtape_layer SHARED layer/xrtape_layer.cpp)
if(MSVC)
    target_sources(xrtape_layer PRIVATE layer/xrtape_layer.def)
endif()
target_include_directories(xrtape_layer PRIVATE "${XRTAPE_OPENXR_INCLUDE}")
set_target_properties(xrtape_layer PROPERTIES
    OUTPUT_NAME "xrtape_layer"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG "${CMAKE_BINARY_DIR}/bin/Debug"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/bin/Release"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${CMAKE_BINARY_DIR}/bin/RelWithDebInfo")

if(MSVC)
    target_compile_options(xrtape_layer PRIVATE /W4 /permissive-)
    target_compile_definitions(xrtape_layer PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()

# The manifest is authored by tools/Install-XrTape.ps1 next to the DLL it
# actually installs, with an absolute library_path. Generating it here as well
# would create a second source of truth for the same file, which is how a stale
# manifest ends up pointing at yesterday's build.
