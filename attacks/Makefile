CXX ?= g++
CXXFLAGS ?= -O2 -g -std=c++17 -Wall -Wextra -Wpedantic -fno-tree-vectorize -fno-pie
LDFLAGS ?= -no-pie

.PHONY: all test
all: prefetch_steer

prefetch_steer: prefetch_steer.cpp
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

test: prefetch_steer
	python3 test_prefetch_steer.py
