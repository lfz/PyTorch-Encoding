#!/usr/bin/env bash
export CXXFLAGS="-std=c++11"
export CFLAGS="-std=c99"

mkdir -p encoding/lib && cd encoding/lib
# compile and install
cmake ..
make
