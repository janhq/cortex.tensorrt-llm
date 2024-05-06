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

#pragma once

#include "tensorrt_llm/runtime/bufferManager.h"
#include "tensorrt_llm/runtime/cudaEvent.h"
#include "tensorrt_llm/runtime/cudaStream.h"
#include "tensorrt_llm/runtime/generationOutput.h"
#include "tensorrt_llm/runtime/gptDecoder.h"
#include "tensorrt_llm/runtime/iGptDecoderBatch.h"
#include "tensorrt_llm/runtime/iTensor.h"

#include <memory>
#include <vector>

namespace tensorrt_llm::runtime
{

//! GPT decoder class with support for in-flight batching
class GptDecoderBatch : public IGptDecoderBatch
{
public:
    using CudaStreamPtr = std::shared_ptr<CudaStream>;
    using TensorPtr = ITensor::SharedPtr;
    using SharedConstPtr = ITensor::SharedConstPtr;

    GptDecoderBatch(std::size_t vocabSize, std::size_t vocabSizePadded, CudaStreamPtr stream);

    //! Setup the decoder before calling `forward()`
    void setup(DecodingMode const& mode, SizeType maxBatchSize, SizeType maxBeamWidth, SizeType maxAttentionWindow,
        SizeType sinkTokenLength, SizeType maxSequenceLength, SizeType maxTokensPerStep, bool fusedDecoder,
        nvinfer1::DataType dtype, ModelConfig const& modelConfig) override;

    void newBatch(
        GenerationInput const& inputs, GenerationOutput const& outputs, SamplingConfig const& samplingConfig) override;

    void newRequests(std::vector<SizeType> const& seqSlots, std::vector<decoder_batch::Request> const& requests,
        std::vector<SamplingConfig> const& samplingConfigs) override;

    TokenPtr forwardAsync(decoder_batch::Output& output, decoder_batch::Input const& input) override;

    void forwardSync(decoder_batch::Token const& token) override;

    void forwardAsync(decoder::Output& output, decoder::Input const& input) override;

    void forwardSync() override;

    //! @return [batchSize], indicators of finished requests
    [[nodiscard]] std::vector<bool> getFinished() const override
    {
        return {mFinished.begin(), mFinished.begin() + mActualBatchSize};
    }

    //! @param batchIdx index of the batch
    //! @returns [maxBeamWidth, maxInputLength + maxNewTokens], contains input token ids and generated token ids without
    //! padding for request `batchIdx`, on gpu
    [[nodiscard]] TensorPtr getOutputIds(SizeType batchIdx) const override
    {
        auto tensor = ITensor::slice(mJointDecodingOutput->ids, batchIdx, 1);
        tensor->squeeze(0);
        return tensor;
    }

    //! @returns [batchSize, maxBeamWidth, maxInputLength + maxNewTokens], contains input token ids and generated token
    //! ids without padding, on gpu
    [[nodiscard]] TensorPtr getOutputIds() const override
    {
        return ITensor::slice(mJointDecodingOutput->ids, 0, mActualBatchSize);
    }

    //! @brief Gather final beam search results for request `batchIdx`.
    //! Result will only be available after event returned.
    [[nodiscard]] CudaEvent finalize(SizeType batchIdx) const override;

    //! @brief Gather final beam search results for all requests.
    void finalize() const override;

    //! @returns [batchSize, maxBeamWidth, maxInputLength + maxNewTokens], contains parent ids collected during beam
    //! search without padding, on gpu
    [[nodiscard]] TensorPtr getParentIds() const override
    {
        return ITensor::slice(mJointDecodingOutput->parentIds, 0, mActualBatchSize);
    }

    //! @returns [batchSize, maxBeamWidth], cumulative log probabilities (per beam), on gpu
    [[nodiscard]] TensorPtr getCumLogProbs() const override
    {
        return ITensor::slice(mJointDecodingOutput->cumLogProbs, 0, mActualBatchSize);
    }

    //! @returns [maxBeamWidth], cumulative log probabilities (per beam), on gpu
    [[nodiscard]] TensorPtr getCumLogProbs(SizeType batchIdx) const override
    {
        auto tensor = ITensor::slice(mJointDecodingOutput->cumLogProbs, batchIdx, 1);
        tensor->squeeze(0);
        return tensor;
    }

    //! @returns [batchSize, maxBeamWidth, maxSequenceLength], log probabilities (per beam), on gpu
    [[nodiscard]] TensorPtr getLogProbs() const override
    {
        return ITensor::slice(mJointDecodingOutput->logProbs, 0, mActualBatchSize);
    }

    //! @returns [maxBeamWidth, maxSequenceLength], log probabilities (per beam), on gpu
    [[nodiscard]] TensorPtr getLogProbs(SizeType batchIdx) const override
    {
        auto tensor = ITensor::slice(mJointDecodingOutput->logProbs, batchIdx, 1);
        tensor->squeeze(0);
        return tensor;
    }

    //! @brief Get maxTokensPerStep tokens generated in the last forward pass
    //! @returns [maxTokensPerStep, batchSize, maxBeamWidth], tokens generated in last forward pass, on gpu
    [[nodiscard]] TensorPtr getAllNewTokens() const override
    {
        return mJointDecodingOutput->newTokensSteps;
    }

    //! @brief Get tokens generated in one step of last forward pass
    //! @param iter The iteration within [0; maxTokensPerStep) for which to get the tokens
    //! @returns [batchSize, beamWidth], tokens generated in `iter` (per beam), on gpu
    [[nodiscard]] TensorPtr getNewTokens(SizeType iter = 0) const override
    {
        TensorPtr newTokensView = ITensor::slice(mJointDecodingOutput->newTokensSteps, iter, 1);
        newTokensView->squeeze(0);
        return ITensor::slice(newTokensView, 0, mActualBatchSize);
    }

    //! @returns [batchSize], the number of generation steps executed on each request
    [[nodiscard]] std::vector<SizeType> getNbSteps() const override
    {
        return {mNbSteps.begin(), mNbSteps.begin() + mActualBatchSize};
    }

    //! @returns [1], number of finished sequences, in pinned host memory
    [[nodiscard]] TensorPtr getNbFinished() const override
    {
        return mFinishedSum;
    }

    //! @returns [batchSize, maxTokensPerStep-1], predicted draft tokens for next step, on gpu
    [[nodiscard]] TensorPtr getNextDraftTokens() const override
    {
        return mJointDecodingOutput->medusaOutputs->medusaNextDraftTokens;
    }

    //! @returns [batchSize + 1], exclusive sum of accepted draft token lengths, on gpu
    [[nodiscard]] TensorPtr getMedusaAcceptedLengthsCumSum() const override
    {
        return mJointDecodingOutput->medusaOutputs->medusaAcceptedLengthsCumSum;
    }

    //! @returns [batchSize * maxMedusaHeads], accepted paths packed into continuous tensor, on gpu
    [[nodiscard]] TensorPtr getMedusaAcceptedPackedPaths() const override
    {
        return mJointDecodingOutput->medusaOutputs->medusaPathsOffsets;
    }

private:
    //! @brief Gather final beam search results for request `batchIdx`.
    [[nodiscard]] CudaEvent postProcessRequest(SizeType batchIdx) const;

    //! @brief Initialize the decoder at `batchIdx` with a new `request`.
    void newRequest(SizeType batchIdx, decoder_batch::Request const& request, SamplingConfig const& samplingConfig);

    //! @brief Allocate buffers for medusa decoding.
    void allocateMedusaBuffers();

    //! @brief Setup buffers for medusa decoding.
    void setupMedusa(ModelConfig const& modelConfig);

    //! @brief Setups decoder internal tensors for new speculative decoding request
    void newRequestSpeculativeDecoding(
        SizeType batchIdx, decoder_batch::Request const& request, SamplingConfig const& samplingConfig);

    //! @brief Setups decoder internal tensors for new Medusa request
    void newRequestMedusa(SizeType batchIdx, decoder_batch::Request const& request);

    //! @brief Asynchronously calls unfused decoder for whole batch in loop
    void forwardAsyncUnfusedDecoder(
        SizeType step, decoder_batch::Output& output, decoder_batch::Input const& input, CudaEvent const& eventStart);

    //! @brief Asynchronously calls fused decoder for whole batch
    void forwardAsyncFusedDecoder(
        SizeType step, decoder_batch::Output& output, decoder_batch::Input const& input, CudaEvent const& eventStart);

private:
    std::size_t const mVocabSize;
    std::size_t const mVocabSizePadded;
    CudaStreamPtr mStream;
    BufferManager mBufferManager;
    TokenPtr mForwardToken;
    CudaEvent mForwardEvent;

    std::vector<CudaStreamPtr> mStreams;
    using GptDecoderPtr = std::unique_ptr<IGptDecoder>;
    std::vector<GptDecoderPtr> mDecoders;
    using DecodingInputPtr = std::unique_ptr<DecodingInput>;
    std::vector<DecodingInputPtr> mDecodingInputs;
    using DecodingOutputPtr = std::unique_ptr<DecodingOutput>;
    std::vector<DecodingOutputPtr> mDecodingOutputs;

    DecodingInputPtr mJointDecodingInput;
    DecodingOutputPtr mJointDecodingOutput;

    std::vector<bool> mAcceptByLogits;
    TensorPtr mNumDraftTokens;
    TensorPtr mCurandStates;

    std::vector<SizeType> mNbSteps;
    std::vector<bool> mFinished;
    TensorPtr mFinishedSum;
    std::vector<SizeType> mMaxNewTokens;
    std::vector<SizeType> mBeamWidths;
    std::vector<SizeType> mGeneratedTokensPerEngineStep;

    TensorPtr mFinishedSteps;   // [maxTokensPerStep, batchSize, beamWidth] finished states of type FinishedState
                                // for each generated token of maxTokensPerStep, on gpu
    TensorPtr mDraftProbs;      // [batchSize, maxDraftTokens+1, beamWidth, vocabPadded], temporary data for speculative
                                // decoding accept by logits kernel, on gpu
    TensorPtr mTargetProbs;     // [batchSize, maxDraftTokens+1, beamWidth, vocabPadded], temporary data for speculative
                                // decoding accept by logits kernel, on gpu
    TensorPtr mDraftTokenIds;   // [batchSize, maxDraftTokens+1], draft token indices, on gpu
    TensorPtr mDraftLogits;     // [batchSize, maxDraftTokens+1, vocabSizePadded], draft token logits, on gpu

    TensorPtr mBatchSlotsSetup; // [maxBatchSize], int32_t, address map, pinned
    TensorPtr mBatchSlotsDecoder;      // [maxBatchSize], int32_t, address map, pinned
    TensorPtr mBatchSlotsAcceptTokens; // [maxBatchSize], int32_t, address map, pinned
    TensorPtr mBatchSlotsAcceptLogits; // [maxBatchSize], int32_t, address map, pinned
    TensorPtr mTargetLogitsPtrs;       // [maxBatchSize], float*, pointers to target logits, pinned
    SizeType mMaxSequenceLength{};
    SizeType mMaxAttentionWindow{};
    SizeType mSinkTokenLength{};
    SizeType mActualBatchSize{};
    SizeType mMaxTokensPerEngineStep{};
    SizeType mMaxStopWordsLen{};
    SizeType mMaxBadWordsLen{};
    // How many tokens for one request can be processed per mDecoders call
    SizeType mMaxTokensPerDecoderStep{};

    bool mFusedDecoder{false};
    bool mUseMedusa{false};
};
} // namespace tensorrt_llm::runtime
