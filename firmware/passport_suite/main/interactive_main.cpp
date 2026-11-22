// Human-driven simulator entry point; production firmware and the regression suite keep their own entry points.
#include "suite.hpp"
#include "esp_timer.h"
#include "driver/uart.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
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
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 4096, 0, 0, nullptr, 0));
    puts("SIM_INTERACTIVE_READY demo_data=1 physical_device=0");
    bool dirty = true;
    bool packet=false,overflow=false;
    static char input[2048];unsigned used=0;
    App previous=App::Count;
    uint64_t lastFrame = 0;
    for (;;) {
        const uint64_t now = esp_timer_get_time() / 1000000;
        uint8_t command;
        const int n = uart_read_bytes(UART_NUM_0, &command, 1, pdMS_TO_TICKS(20));
        if (n == 1&&packet) {
            if(command=='\n'){
                input[used]=0;packet=false;
                cJSON* json=overflow?nullptr:cJSON_Parse(input);
                if(json&&runtime.active()==App::Hermes){
                    const auto* generation=cJSON_GetObjectItem(json,"generation");
                    const auto* kind=cJSON_GetObjectItem(json,"kind");
                    if(cJSON_IsNumber(generation)&&generation->valuedouble==runtime.generation()&&cJSON_IsString(kind)){
                        const auto* text=cJSON_GetObjectItem(json,"text");
                        if(!strcmp(kind->valuestring,"say")&&cJSON_IsString(text))runtime.hermesMessage(text->valuestring);
                        else if(!strcmp(kind->valuestring,"reply")){
                            auto& h=runtime.model.page.hermes;
                            const auto* turn=cJSON_GetObjectItem(json,"turn");
                            if(cJSON_IsNumber(turn)&&(turn->valuedouble>=h.turn||cJSON_IsTrue(cJSON_GetObjectItem(json,"reset")))){
                                const auto* heard=cJSON_GetObjectItem(json,"heard");
                                const auto* state=cJSON_GetObjectItem(json,"state");
                                if(cJSON_IsString(text))hermesText(h.answer,sizeof(h.answer),text->valuestring);
                                if(cJSON_IsString(heard))hermesText(h.heard,sizeof(h.heard),heard->valuestring);
                                if(cJSON_IsString(state))hermesText(runtime.model.liveState,sizeof(runtime.model.liveState),state->valuestring);
                                h.turn=static_cast<uint32_t>(turn->valuedouble);h.busy=cJSON_IsTrue(cJSON_GetObjectItem(json,"busy"));
                                h.truncated=cJSON_IsTrue(cJSON_GetObjectItem(json,"truncated"));h.remote=cJSON_IsTrue(cJSON_GetObjectItem(json,"remote"));h.locked=cJSON_IsTrue(cJSON_GetObjectItem(json,"locked"));h.scroll=0;
                                runtime.model.livePhase=LivePhase::Ready;
                                printf("SIM_HERMES_APPLIED turn=%lu busy=%u\n",static_cast<unsigned long>(h.turn),unsigned(h.busy));
                            }
                        }
                    }
                }
                cJSON_Delete(json);dirty=true;
            }else if(used+1<sizeof(input))input[used++]=command;else overflow=true;
        } else if(n==1&&command=='@') {packet=true;used=0;overflow=false;}
        else if (n == 1) {
            if(command=='c'&&runtime.active()==App::Hermes){puts("SIM_HERMES_INPUT");fflush(stdout);continue;}
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
        if(packet)continue;
        if(runtime.active()!=previous){
            previous=runtime.active();
            if(previous==App::Hermes)printf("SIM_HERMES_ENTER {\"generation\":%lu}\n",static_cast<unsigned long>(runtime.generation()));
        }
        if(runtime.active()==App::Hermes){
            auto& h=runtime.model.page.hermes;
            if(h.outgoing){
                h.outgoing=false;
                auto* json=cJSON_CreateObject();cJSON_AddNumberToObject(json,"generation",runtime.generation());
                cJSON_AddNumberToObject(json,"turn",h.turn);cJSON_AddStringToObject(json,"text",h.heard);
                char* data=cJSON_PrintUnformatted(json);if(data){printf("SIM_HERMES_REQUEST %s\n",data);cJSON_free(data);}cJSON_Delete(json);
            }
            if(h.next){h.next=false;printf("SIM_HERMES_NEXT {\"generation\":%lu}\n",static_cast<unsigned long>(runtime.generation()));}
            if(h.reset){h.reset=false;printf("SIM_HERMES_NEW {\"generation\":%lu}\n",static_cast<unsigned long>(runtime.generation()));}
        }
        runtime.tick(now);
        runtime.poll();
        if (dirty || now != lastFrame) {
            uiRender(runtime);
            printf("SIM_STATE app=%s selection=%u generation=%lu\n", profile(runtime.active()).id, runtime.selection(),static_cast<unsigned long>(runtime.generation()));
            uiCapture("interactive");
            fflush(stdout);
            lastFrame = now;
            dirty = false;
        }
    }
}
