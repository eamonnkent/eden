add_executable(
  edenfs
  service/main.cpp
)
target_include_directories(
  edenfs
  PUBLIC
    ${CMAKE_SOURCE_DIR}
    ${CMAKE_BINARY_DIR}
)
target_link_libraries(
  edenfs
    eden_service
    Folly::folly
)

add_subdirectory(config)
add_subdirectory(fuse)
add_subdirectory(inodes)
add_subdirectory(journal)
add_subdirectory(model)
add_subdirectory(rocksdb)
add_subdirectory(service)
add_subdirectory(sqlite)
add_subdirectory(store)
add_subdirectory(takeover)
add_subdirectory(utils)
