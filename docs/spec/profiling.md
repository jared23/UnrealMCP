# Spec: Profiling (`/docs/reference/profiling`, 11 cmds — UE 5.7+)

Clean-room. Unreal Insights tracing + live frame timing. Implement over trace/Insights automation.

- `performance_start_trace` — record .utrace (channels/presets) — preset(cpu/gpu/render/memory/loading/network/animation…); channels; filter
- `performance_stop_trace` — end trace (optional auto-load)
- `performance_analyze_insight` — diagnostic queries — query(diagnose/flame/hotpath/spikes/bound/breakdown/calltree/gpu_sync/gc/memory_growth/histogram/objects…); filter; thread; frame_index; count; threshold_ms
- `performance_list_channels` — list channels + presets
- `performance_toggle_channel` — enable/disable channel mid-trace
- `performance_trace_bookmark` — named timeline marker
- `performance_trace_snapshot` — write tail buffer without stopping
- `performance_trace_screenshot` — embed viewport screenshot in trace
- `performance_trace_object` — register actors for per-frame property recording
- `performance_live_start` — sample frame timing (no trace overhead)
- `performance_live_stop` — retrieve multi-frame timing distribution stats
