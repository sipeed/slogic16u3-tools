cmake_minimum_required(VERSION 3.13...3.16 FATAL_ERROR)
project(slogic_cli VERSION 0.0.1 LANGUAGES C)

add_executable(${CMAKE_PROJECT_NAME} src/main.c)
target_compile_features(${CMAKE_PROJECT_NAME} PRIVATE c_std_11)

find_package(PkgConfig REQUIRED)
    pkg_check_modules(libusb REQUIRED IMPORTED_TARGET libusb-1.0)

target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE PkgConfig::libusb)