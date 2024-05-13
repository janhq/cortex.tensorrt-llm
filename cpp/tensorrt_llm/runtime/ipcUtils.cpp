/*
 * Copyright (c) 2022-2024, NVIDIA CORPORATION.  All rights reserved.
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

#include "tensorrt_llm/runtime/ipcUtils.h"

#include "tensorrt_llm/common/cudaUtils.h"
#include "tensorrt_llm/common/customAllReduceUtils.h"
#include "tensorrt_llm/common/mpiUtils.h"

#include <NvInferRuntimeBase.h>
#include <cstddef>

namespace tensorrt_llm::runtime
{

namespace
{
void setPeerAccess(WorldConfig const& worldConfig, bool enable)
{
    TLLM_LOG_TRACE("%s start", __PRETTY_FUNCTION__);
    auto const srcNode = worldConfig.getTensorParallelRank();

    for (SizeType destNode = 0; destNode < worldConfig.getTensorParallelism(); destNode++)
    {
        if (destNode == srcNode)
        {
            continue;
        }

        int canAccessPeer{0};
        TLLM_CUDA_CHECK(cudaDeviceCanAccessPeer(&canAccessPeer, srcNode, destNode));

        if (enable)
        {
            cudaDeviceEnablePeerAccess(destNode, 0);
        }
        else
        {
            cudaDeviceDisablePeerAccess(destNode);
        }
        auto const error = cudaGetLastError();
        if (error != cudaErrorPeerAccessAlreadyEnabled && error != cudaErrorPeerAccessNotEnabled)
        {
            TLLM_CUDA_CHECK(error);
        }
    }
    TLLM_LOG_TRACE("%s stop", __PRETTY_FUNCTION__);
}
} // namespace

IpcMemory::IpcMemory(std::size_t bufferSize, BufferManager const& manager, WorldConfig const& worldConfig)
    : mTpRank(worldConfig.getTensorParallelRank())
    , mCommPtrs(worldConfig.getTensorParallelism())
{
    allocateIpcMemory(bufferSize, manager, worldConfig);
}

void IpcMemory::allocateIpcMemory(std::size_t bufferSize, BufferManager const& manager, WorldConfig const& worldConfig)
{
    TLLM_LOG_TRACE("%s start", __PRETTY_FUNCTION__);

    // cudaIpcGetMemHandle only works with allocation created with cudaMalloc
    mBuffer = BufferManager::gpuSync(bufferSize, nvinfer1::DataType::kUINT8);
    manager.setZero(*mBuffer);
    auto* bufferPtr = mBuffer->data();

    cudaIpcMemHandle_t localHandle;
    TLLM_CUDA_CHECK(cudaIpcGetMemHandle(&localHandle, bufferPtr));

    auto const ppRank = worldConfig.getPipelineParallelRank();
    auto const comm = COMM_SESSION.split(ppRank, mTpRank);
    std::vector<char> serialHandles(CUDA_IPC_HANDLE_SIZE * worldConfig.getTensorParallelism(), 0);
    comm.allgather(&localHandle.reserved, serialHandles.data(), CUDA_IPC_HANDLE_SIZE, mpi::MpiType::kBYTE);

    std::vector<cudaIpcMemHandle_t> handles(worldConfig.getTensorParallelism());
    for (size_t i = 0; i < handles.size(); ++i)
    {
        memcpy(handles[i].reserved, &serialHandles[i * CUDA_IPC_HANDLE_SIZE], CUDA_IPC_HANDLE_SIZE);
    }

    for (std::size_t nodeId = 0; nodeId < handles.size(); nodeId++)
    {
        if (nodeId == static_cast<std::size_t>(mTpRank))
        {
            mCommPtrs.at(nodeId) = bufferPtr;
        }
        else
        {
            uint8_t* foreignBuffer{nullptr};
            TLLM_CUDA_CHECK(cudaIpcOpenMemHandle(
                reinterpret_cast<void**>(&foreignBuffer), handles[nodeId], cudaIpcMemLazyEnablePeerAccess));
            mCommPtrs.at(nodeId) = foreignBuffer;
        }
    }
    TLLM_LOG_TRACE("%s stop", __PRETTY_FUNCTION__);
}

IpcMemory::~IpcMemory()
{
    destroyIpcMemory();
}

void IpcMemory::destroyIpcMemory()
{
    TLLM_LOG_TRACE("%s start", __PRETTY_FUNCTION__);

    for (std::size_t nodeId = 0; nodeId < mCommPtrs.size(); ++nodeId)
    {
        if (nodeId != static_cast<std::size_t>(mTpRank))
        {
            TLLM_CUDA_CHECK(cudaIpcCloseMemHandle(mCommPtrs.at(nodeId)));
        }
    }
    TLLM_LOG_TRACE("%s stop", __PRETTY_FUNCTION__);
}

AllReduceBuffers::AllReduceBuffers(SizeType maxBatchSize, SizeType maxBeamWidth, SizeType maxSequenceLength,
    SizeType hiddenSize, BufferManager const& manager, WorldConfig const& worldConfig)
{
    TLLM_LOG_TRACE("%s start", __PRETTY_FUNCTION__);
    setPeerAccess(worldConfig, true);

    auto const tpSize = worldConfig.getTensorParallelism();

    auto const bufferSize = tpSize
        * std::min(
            static_cast<std::size_t>(maxBatchSize) * maxBeamWidth * maxSequenceLength * hiddenSize * sizeof(float),
            utils::customAllReduceUtils::getMaxRequiredWorkspaceSize(tpSize));
    auto const flagsSize = IpcMemory::FLAGS_SIZE * tpSize;

    for (auto size : {bufferSize, bufferSize, flagsSize, flagsSize})
    {
        mIpcMemoryHandles.emplace_back(size, manager, worldConfig);
    }

    mAllReduceCommPtrs = BufferManager::cpu(
        ITensor::makeShape({static_cast<SizeType>(mIpcMemoryHandles.size()) * tpSize}), nvinfer1::DataType::kINT64);
    auto commPtrs = BufferRange<void*>(*mAllReduceCommPtrs);

    for (std::size_t memIdx = 0; memIdx < mIpcMemoryHandles.size(); memIdx++)
    {
        auto const& memCommPtrs = mIpcMemoryHandles[memIdx].getCommPtrs();
        TLLM_CHECK(memCommPtrs.size() == static_cast<std::size_t>(tpSize));
        std::copy(memCommPtrs.begin(), memCommPtrs.end(), commPtrs.begin() + memIdx * tpSize);
    }
    TLLM_LOG_TRACE("%s stop", __PRETTY_FUNCTION__);
}

} // namespace tensorrt_llm::runtime
