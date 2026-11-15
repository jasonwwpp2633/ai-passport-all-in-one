// Human-driven simulator entry point; production firmware and the regression suite keep their own entry points.
#include "suite.hpp"
#include "esp_timer.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <sys/time.h>

extern "C" void app_main() {
    using namespace passport;
    ESP_ERROR_CHECK(nvs_flash_init());
    static SystemSettings settings;
    SettingsStore store;
    bool created = false;
    if (!store.loadOrCreate(settings, created)) abort();
    static Runtime runtime;
    uiInit();
    if (!runtime.init(settings)) abort();
    uiResourcesReady(true);
    // Explicit demo values, no account, network, microphone or physical device access.
    runtime.model.batteryKnown = true;
    runtime.model.batteryPercent = 85;
    runtime.model.weatherReading.update("杭州", "多云", 24, 20, 28, 65, 51, 161);
    setenv("TZ", "CST-8", 1);
    tzset();
    timeval clock{1794744000, 0};
    settimeofday(&clock, nullptr);
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 1024, 0, 0, nullptr, 0));
    puts("SIM_INTERACTIVE_READY demo_data=1 physical_device=0");
    bool dirty = true;
    uint64_t lastFrame = 0;
    for (;;) {
        const uint64_t now = esp_timer_get_time() / 1000000;
        uint8_t command;
        const int n = uart_read_bytes(UART_NUM_0, &command, 1, pdMS_TO_TICKS(20));
        if (n == 1) {
            switch (command) {
                case 'u': runtime.key(Key::Up, now); break;
                case 'd': runtime.key(Key::Down, now); break;
                case 'c': runtime.key(Key::Confirm, now); break;
                case 'b': runtime.key(Key::Back, now); break;
                case 'U': runtime.longKey(true, now); break;
                case 'D': runtime.longKey(false, now); break;
                case 'r': break;
                default: continue;
            }
            dirty = true;
        }
        runtime.tick(now);
        runtime.poll();
        if (dirty || now != lastFrame) {
            uiRender(runtime);
            printf("SIM_STATE app=%s selection=%u\n", profile(runtime.active()).id, runtime.selection());
            uiCapture("interactive");
            fflush(stdout);
            lastFrame = now;
            dirty = false;
        }
    }
}
