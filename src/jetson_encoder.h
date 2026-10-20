// Sunshine Jetson fork: native hardware backend added 2026-10-20.
// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace jetson {
  struct settings {
    int input_width, input_height, width, height, fps, bitrate_kbps;
    int reference_frames, colorspace;
    bool full_range;
  };

  struct encoded_frame {
    std::vector<std::uint8_t> bytes;
    bool keyframe = false;
  };

  // Jetson's VIC performs BGRx -> NV12 conversion; NVENC encodes the NVMM surface.
  // The pipeline is synchronous (one submitted frame, one access unit returned).
  class encoder {
  public:
    encoder();
    ~encoder();
    encoder(const encoder &) = delete;
    encoder &operator=(const encoder &) = delete;
    bool init(const settings &config, std::string &error);
    bool encode(const std::uint8_t *bgrx, int stride, bool force_idr,
                encoded_frame &output, std::string &error);

  private:
    struct impl;
    std::unique_ptr<impl> state;
  };
}
