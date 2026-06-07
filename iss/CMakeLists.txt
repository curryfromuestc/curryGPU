cmake_minimum_required(VERSION 3.20)
project(currygpu_iss LANGUAGES CXX)

find_package(Python3 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG REQUIRED)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CURRYGPU_GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated" CACHE PATH "Generated decoder output")
set(CURRYGPU_ISA_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../isa" CACHE PATH "Public ISA component root")
set(CURRYGPU_ISA_LAYOUT "sample" CACHE STRING "ISA layout to use for generated public sample artifacts")
file(MAKE_DIRECTORY "${CURRYGPU_GENERATED_DIR}")

get_filename_component(CURRYGPU_ISA_ROOT "${CURRYGPU_ISA_ROOT}" ABSOLUTE)
if(NOT EXISTS "${CURRYGPU_ISA_ROOT}/currygpu/isa/codegen.py")
    message(FATAL_ERROR "CURRYGPU_ISA_ROOT must point to the public isa component with currygpu/isa/codegen.py")
endif()

add_custom_command(
    OUTPUT
        "${CURRYGPU_GENERATED_DIR}/decode.gen.cpp"
        "${CURRYGPU_GENERATED_DIR}/decoded_inst.gen.h"
        "${CURRYGPU_GENERATED_DIR}/emit.gen.py"
        "${CURRYGPU_GENERATED_DIR}/isa_ir.json"
    COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${CURRYGPU_ISA_ROOT}:${CMAKE_CURRENT_SOURCE_DIR}" "${Python3_EXECUTABLE}" -m currygpu.isa.codegen --layout "${CURRYGPU_ISA_LAYOUT}" --out "${CURRYGPU_GENERATED_DIR}"
    DEPENDS
        "${CURRYGPU_ISA_ROOT}/currygpu/isa/codegen.py"
        "${CURRYGPU_ISA_ROOT}/currygpu/isa/schema.py"
        "${CURRYGPU_ISA_ROOT}/currygpu/isa/layout/__init__.py"
        "${CURRYGPU_ISA_ROOT}/currygpu/isa/layout/sample.py"
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    COMMENT "Generate ISA decoder files"
)

add_custom_target(currygpu_iss_codegen
    DEPENDS
        "${CURRYGPU_GENERATED_DIR}/decode.gen.cpp"
        "${CURRYGPU_GENERATED_DIR}/decoded_inst.gen.h"
        "${CURRYGPU_GENERATED_DIR}/emit.gen.py"
        "${CURRYGPU_GENERATED_DIR}/isa_ir.json"
)

add_executable(currygpu_decode_smoke tests/cpp/decode_smoke.cpp "${CURRYGPU_GENERATED_DIR}/decode.gen.cpp")
target_include_directories(currygpu_decode_smoke PRIVATE "${CURRYGPU_GENERATED_DIR}")

pybind11_add_module(_native binding/native.cpp "${CURRYGPU_GENERATED_DIR}/decode.gen.cpp")
add_dependencies(_native currygpu_iss_codegen)
target_include_directories(_native PRIVATE "${CURRYGPU_GENERATED_DIR}")
set_target_properties(_native PROPERTIES OUTPUT_NAME "_native")
add_custom_command(TARGET _native POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy "$<TARGET_FILE:_native>" "${CMAKE_CURRENT_SOURCE_DIR}/currygpu/iss/$<TARGET_FILE_NAME:_native>"
    COMMENT "Copy native extension beside source package for local tests"
)
install(TARGETS _native LIBRARY DESTINATION currygpu/iss)

enable_testing()
add_test(NAME currygpu_decode_smoke COMMAND currygpu_decode_smoke)
