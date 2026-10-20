// Sunshine Jetson fork: hardware validation added 2026-10-20.
// SPDX-License-Identifier: GPL-3.0-only
#include "jetson_encoder.h"
#include <chrono>
#include <fstream>
#include <iostream>
#include <vector>

int main(int argc, char **argv) {
  const int width = 1920, height = 1080, frames = 90;
  jetson::encoder encoder;
  std::string error;
  if (!encoder.init({width, height, width, height, 30, 10000, 1, 0, false}, error)) {
    std::cerr << error << '\n';
    return 1;
  }
  std::vector<uint8_t> pixels(width * height * 4, 0);
  for (int y=0;y<height;++y)
    for (int x=0;x<width;++x) {
      auto offset=(y*width+x)*4;
      pixels[offset]=x*255/width;
      pixels[offset+1]=y*255/height;
      pixels[offset+2]=128;
    }
  std::ofstream output(argc > 1 ? argv[1] : "jetson-probe.h264", std::ios::binary);
  if (!output) return 2;
  auto start=std::chrono::steady_clock::now();
  double steady_ms=0;
  for (int i=0;i<frames;++i) {
    bool force=i==0 || i==30 || i==60;
    auto before=std::chrono::steady_clock::now();
    jetson::encoded_frame encoded;
    if (!encoder.encode(pixels.data(), width*4, force, encoded, error)) {
      std::cerr << "frame " << i << ": " << error << '\n';
      return 3;
    }
    auto ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-before).count();
    if (i) steady_ms+=ms;
    bool idr=false,sps=false,pps=false;
    for (size_t j=0;j+4<encoded.bytes.size();++j)
      if (encoded.bytes[j]==0 && encoded.bytes[j+1]==0 && encoded.bytes[j+2]==1) {
        int type=encoded.bytes[j+3]&31;
        idr|=type==5; sps|=type==7; pps|=type==8;
      }
    if (force && (!idr || !sps || !pps)) {
      std::cerr << "IDR/SPS/PPS missing at frame " << i << '\n';
      return 4;
    }
    if (force) std::cout << "Verified IDR + SPS/PPS at frame " << i << " in " << ms << " ms\n";
    output.write(reinterpret_cast<const char*>(encoded.bytes.data()),encoded.bytes.size());
  }
  double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
  std::cout << "Encoded " << frames << " frames in " << seconds << " s; throughput "
            << frames/seconds << " fps; steady average " << steady_ms/(frames-1) << " ms/frame\n";
  return 0;
}
