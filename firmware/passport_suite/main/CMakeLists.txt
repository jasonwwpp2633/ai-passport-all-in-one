set(SIM_ENTRY "sim_main.cpp")
if(PASSPORT_SIM_INTERACTIVE)
  set(SIM_ENTRY "interactive_main.cpp")
endif()
idf_component_register(SRCS "suite.cpp" "domain.cpp" "ui.cpp" "home_ui.cpp" "status_bar.cpp" "ui_font.cpp" "status_font.cpp" "lunar.cpp" "settings.cpp" "identity.cpp" "ble_model.cpp" ${SIM_ENTRY}
  INCLUDE_DIRS "." REQUIRES suite_lvgl esp_partition esp_wifi esp_event esp_netif nvs_flash mbedtls esp_timer esp_driver_uart)
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_20)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
# SIM_ENGLISH=1 scripts/build_passport_suite.sh: the simulator runs in English and logs text left in Chinese.
if(PASSPORT_SIM_ENGLISH)
  target_compile_definitions(${COMPONENT_LIB} PRIVATE PASSPORT_SIM_ENGLISH=1)
endif()
