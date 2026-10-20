// Sunshine Jetson fork: native hardware backend added 2026-10-20.
// SPDX-License-Identifier: GPL-3.0-only
#include "jetson_encoder.h"

#include <algorithm>
#include <cstring>
#include <mutex>
#include <sstream>
#include <gst/app/gstappsrc.h>
#include <gst/app/gstappsink.h>
#include <gst/video/video.h>

namespace jetson {
  struct encoder::impl {
    GstElement *pipeline = nullptr, *source = nullptr, *sink = nullptr, *hardware = nullptr;
    settings config {};
    std::uint64_t frame_index = 0;

    ~impl() {
      if (pipeline) gst_element_set_state(pipeline, GST_STATE_NULL);
      if (source) gst_object_unref(source);
      if (sink) gst_object_unref(sink);
      if (hardware) gst_object_unref(hardware);
      if (pipeline) gst_object_unref(pipeline);
    }

    std::string bus_error(const char *fallback) {
      auto bus = gst_element_get_bus(pipeline);
      auto message = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR);
      gst_object_unref(bus);
      if (!message) return fallback;
      GError *error = nullptr;
      gchar *debug = nullptr;
      gst_message_parse_error(message, &error, &debug);
      std::string result = error ? error->message : fallback;
      if (debug) result += std::string(" (") + debug + ")";
      g_clear_error(&error);
      g_free(debug);
      gst_message_unref(message);
      return result;
    }
  };

  encoder::encoder() = default;
  encoder::~encoder() = default;

  bool encoder::init(const settings &config, std::string &error) {
    if (state) {
      error = "Jetson encoder was already initialized";
      return false;
    }
    if (config.input_width <= 0 || config.input_height <= 0 ||
        config.width <= 0 || config.height <= 0 || config.width % 2 || config.height % 2 ||
        config.fps <= 0 || config.fps > 240 || config.bitrate_kbps <= 0 ||
        config.bitrate_kbps > 200000 ||
        static_cast<int64_t>(config.input_width) * config.height !=
          static_cast<int64_t>(config.input_height) * config.width) {
      error = "Jetson H.264 requires even dimensions and matching input/output aspect ratios";
      return false;
    }
    static std::once_flag init_once;
    static bool initialized = false;
    static std::string init_error;
    std::call_once(init_once, [] {
      GError *error = nullptr;
      initialized = gst_init_check(nullptr, nullptr, &error);
      if (error) {
        init_error = error->message;
        g_clear_error(&error);
      }
    });
    if (!initialized) {
      error = init_error;
      return false;
    }
    auto candidate = std::make_unique<impl>();
    candidate->config = config;
    GstVideoColorimetry color {};
    gst_video_colorimetry_from_string(&color,
      config.colorspace == 1 ? "bt709" : config.colorspace == 2 ? "bt2020" : "bt601");
    color.range = config.full_range ? GST_VIDEO_COLOR_RANGE_0_255 : GST_VIDEO_COLOR_RANGE_16_235;
    gchar *color_name = gst_video_colorimetry_to_string(&color);
    std::ostringstream description;
    description
      << "appsrc name=source is-live=true format=time block=true max-bytes="
      << static_cast<int64_t>(config.input_width) * config.input_height * 4 * 2
      << " ! video/x-raw,format=BGRx,width=" << config.input_width
      << ",height=" << config.input_height << ",framerate=" << config.fps << "/1"
      << " ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12,width=" << config.width
      << ",height=" << config.height << ",colorimetry=" << color_name
      << " ! nvv4l2h264enc name=hardware bitrate=" << config.bitrate_kbps * 1000
      << " control-rate=1 preset-level=1 maxperf-enable=true profile=2 num-B-Frames=0"
      << " num-Ref-Frames=" << std::max(1, std::min(8, config.reference_frames))
      << " iframeinterval=" << config.fps * 10 << " idrinterval=" << config.fps * 10
      << " insert-sps-pps=true insert-vui=true"
      << " ! h264parse config-interval=-1"
      << " ! video/x-h264,stream-format=byte-stream,alignment=au"
      << " ! appsink name=sink sync=false max-buffers=2 drop=false";
    g_free(color_name);
    GError *parse_error = nullptr;
    candidate->pipeline = gst_parse_launch(description.str().c_str(), &parse_error);
    if (parse_error || !candidate->pipeline) {
      error = parse_error ? parse_error->message : "Could not create Jetson pipeline";
      g_clear_error(&parse_error);
      return false;
    }
    candidate->source = gst_bin_get_by_name(GST_BIN(candidate->pipeline), "source");
    candidate->sink = gst_bin_get_by_name(GST_BIN(candidate->pipeline), "sink");
    candidate->hardware = gst_bin_get_by_name(GST_BIN(candidate->pipeline), "hardware");
    if (!candidate->source || !candidate->sink || !candidate->hardware ||
        gst_element_set_state(candidate->pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
      error = candidate->bus_error("Could not start Jetson pipeline");
      return false;
    }
    state = std::move(candidate);
    return true;
  }

  bool encoder::encode(const std::uint8_t *bgrx, int stride, bool force_idr,
                       encoded_frame &output, std::string &error) {
    output.bytes.clear();
    output.keyframe = false;
    if (!state || !bgrx || stride < state->config.input_width * 4) {
      error = "Invalid Jetson input frame";
      return false;
    }
    auto &s = *state;
    auto row_bytes = static_cast<size_t>(s.config.input_width) * 4;
    auto buffer = gst_buffer_new_allocate(nullptr, row_bytes * s.config.input_height, nullptr);
    if (!buffer) {
      error = "Unable to allocate Jetson input buffer";
      return false;
    }
    GstMapInfo map {};
    if (!gst_buffer_map(buffer, &map, GST_MAP_WRITE)) {
      gst_buffer_unref(buffer);
      error = "Unable to map Jetson input buffer";
      return false;
    }
    for (int y = 0; y < s.config.input_height; ++y)
      std::memcpy(map.data + y * row_bytes, bgrx + static_cast<size_t>(y) * stride, row_bytes);
    gst_buffer_unmap(buffer, &map);
    GST_BUFFER_PTS(buffer) = gst_util_uint64_scale(s.frame_index, GST_SECOND, s.config.fps);
    GST_BUFFER_DURATION(buffer) = gst_util_uint64_scale(1, GST_SECOND, s.config.fps);
    if (force_idr && s.frame_index > 0) {
      // JetPack 5 acknowledges the generic GstForceKeyUnit event without
      // immediately producing an IDR. Its native action invokes the V4L2 control.
      if (!g_signal_lookup("force-IDR", G_OBJECT_TYPE(s.hardware))) {
        gst_buffer_unref(buffer);
        error = "Jetson encoder has no force-IDR action";
        return false;
      }
      g_signal_emit_by_name(s.hardware, "force-IDR");
    }
    if (gst_app_src_push_buffer(GST_APP_SRC(s.source), buffer) != GST_FLOW_OK) {
      error = s.bus_error("Could not submit a frame to Jetson encoder");
      return false;
    }
    // Startup can initialize the driver; steady-state must not accumulate latency.
    auto timeout = s.frame_index == 0 ? 3 * GST_SECOND : GST_SECOND;
    auto sample = gst_app_sink_try_pull_sample(GST_APP_SINK(s.sink), timeout);
    if (!sample) {
      error = s.bus_error("Timed out waiting for Jetson H.264 access unit");
      return false;
    }
    auto encoded = gst_sample_get_buffer(sample);
    if (!encoded || !gst_buffer_map(encoded, &map, GST_MAP_READ)) {
      gst_sample_unref(sample);
      error = "Unable to read Jetson output";
      return false;
    }
    output.bytes.assign(map.data, map.data + map.size);
    output.keyframe = !GST_BUFFER_FLAG_IS_SET(encoded, GST_BUFFER_FLAG_DELTA_UNIT);
    gst_buffer_unmap(encoded, &map);
    gst_sample_unref(sample);
    ++s.frame_index;
    if (output.bytes.empty() || (force_idr && !output.keyframe)) {
      error = "Jetson encoder did not produce the requested IDR access unit";
      return false;
    }
    return true;
  }
}
