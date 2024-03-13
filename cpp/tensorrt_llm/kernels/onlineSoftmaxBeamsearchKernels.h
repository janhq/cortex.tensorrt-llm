/*
 * Copyright (c) 2020-2023, NVIDIA CORPORATION.  All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#pragma once

#include "tensorrt_llm/kernels/beamSearchTopkKernels.h"
#include "tensorrt_llm/kernels/decodingCommon.h"

namespace tensorrt_llm
{
namespace kernels
{

template <typename T>
void invokeTopkSoftMax(T const* log_probs, T const* bias, void* tmp_storage, int const temp_storage_size,
    BeamHypotheses& beam_hyps, cudaStream_t stream);

} // namespace kernels
} // namespace tensorrt_llm
