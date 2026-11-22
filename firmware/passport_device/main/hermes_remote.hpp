#pragma once
#include "hermes.hpp"
#include <cstddef>
#include <cstdint>

namespace passport {
// Separate, versioned records: never change SystemSettings/config_v1.
struct HermesConfig {
    uint32_t version{1}, revision{};
    char origin[192]{}, token[97]{};
    uint8_t enabled{};
};
enum class HermesTaskPhase:uint8_t { Idle, Pending, Unknown, Done, Failed };
struct HermesTask {
    uint32_t version{1}, revision{}, turn{}, cursor{};
    char session[33]{}, request[33]{}, heard[384]{};
    HermesTaskPhase phase{HermesTaskPhase::Idle};
};
struct HermesHooks {
    void* context{};
    bool (*save)(void*,const HermesTask&){};
    void (*uuid)(void*,char*){};
    int (*http)(void*,const char*,const char*,const char*,char*,size_t){};
};
bool hermesOrigin(const char*);
bool hermesToken(const char*);
bool hermesConfigValid(const HermesConfig&);
bool hermesTaskValid(const HermesTask&);
class HermesRemote {
    HermesConfig config_{};
    HermesTask task_{};
    HermesView view_{};
    char state_[64]{"远程模式"};
    uint32_t next_{};
    bool broken_{};
    void state(const char*);
    bool commit(const HermesTask&,const HermesHooks&);
    void accept(const char*,const HermesHooks&);
public:
    void restore(const HermesConfig&,const HermesTask&,bool valid);
    bool enabled()const{return config_.enabled;}
    bool pending()const{return task_.phase==HermesTaskPhase::Pending||task_.phase==HermesTaskPhase::Unknown;}
    void step(const char* operation,const char* input,uint32_t turn,const HermesHooks&);
    const HermesView& view()const{return view_;}
    const char* state()const{return state_;}
};
// ESP-IDF storage adapter. Config changes take effect after a device restart.
bool hermesReadConfig(HermesConfig&);
bool hermesReadTask(HermesTask&);
bool hermesWriteConfig(HermesConfig&);
bool hermesWriteTask(const HermesTask&);
bool hermesResolveTask();
}
