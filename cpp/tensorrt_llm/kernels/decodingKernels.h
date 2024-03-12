/*
 * Copyright (c) 2019-2024, NVIDIA CORPORATION.  All rights reserved.
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

#include "gptKernels.h"
#include "tensorrt_llm/kernels/decodingCommon.h"
#include "tensorrt_llm/runtime/common.h"
#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <curand_kernel.h>

namespace tensorrt_llm
{

namespace kernels
{

struct gatherTreeParam
{
    // TODO rename the parameters
    int32_t* beams = nullptr;              // [batchSize, beamWidth, maxSeqLen], workspace to put intermediate outputIds
    int32_t* sequenceLengths = nullptr;    // [batchSize, beamWidth], total lengths of each query
    int32_t maxSequenceLengthFinalStep = 0;
    int32_t const* inputLengths = nullptr; // [batchSize, beamWidth]
    // response input lengths (used to slice the ids during postprocessing)
    int32_t* responseInputLengths = nullptr;
    int32_t maxSeqLen = 0;
    int32_t batchSize = 0;
    int32_t beamWidth = 0;
    int32_t const* stepIds = nullptr;   // [maxSeqLen, batchSize, beamWidth]
    int32_t const* parentIds = nullptr; // [maxSeqLen, batchSize, beamWidth]
    int32_t const* endTokens = nullptr; // [batchSize], end token ids of each query
    int32_t* outputIds = nullptr;       // the buffer to put finalized ids
    cudaStream_t stream;
    float* cumLogProbs = nullptr;       // [batchSize, beamWidth]
    float lengthPenalty = 1.0f;
    int earlyStopping = 1;
};

/*
Do gatherTree on beam search to get final result.
*/
void invokeGatherTree(gatherTreeParam param);

void invokeFinalize(int32_t* outputIds, int32_t* sequenceLengths, float* cumLogProbs, float* outputLogProbs,
    int32_t const* topKOutputIds, int32_t const* topKSequenceLengths, float const* scores, float const* topKCumLogProbs,
    float const* topKLogProbs, int32_t const* numBeams, int32_t const* inputLengths, int32_t beamWidth,
    int32_t maxSeqLen, int32_t batchSize, cudaStream_t stream);

void invokeInitializeOutput(
    int32_t* outputIds, int32_t const* endIds, int batchBeam, int maxSeqLen, cudaStream_t stream);

void invokeCopyNextStepIds(int32_t* nextStepIds, int32_t** outputIdsPtr, int32_t const* sequenceLengths,
    int32_t const* batchSlots, int32_t batchSize, int32_t beamWidth, int32_t maxSeqLen, cudaStream_t stream);

//! \brief Accepts or rejects draft tokens based on the equality of draft and target tokens
//! for speculative decoding. Target token is accepted if targetToken == draftToken.
//! If number of accepted tokens N < maxDraftTokens, then function accepts N + 1 tokens of target model.
//! sequenceLengths, finishedSum and finishedFinal are modified accordingly.
//!
//! \param draftIds input buffer [batchSize, maxDraftTokens].
//! Indices of the draft tokens.
//! \param targetIds input buffer [batchSize, maxSeqLen]. Indices of the tokens decoded by the target model
//! \param contextLengths input buffer [batchSize]. Context lengths of the requests without draft tokens
//! \param numsDraftTokens input buffer [batchSize]. Number of draft tokens per request
//! \param sequenceLengths input/output buffer [batchSize] sequence lengths of the requests in batch
//! Modified in-place according to the accepted/rejected tokens
//! \param finished input buffer [maxDraftTokens + 1, batchSize] finished states at each decoding iteration
//! \param finishedFinal output buffer [batchSize] finished states after accepting/rejecting tokens
//! \param finishedSum output buffer [1] total number of requests in batch that finished the execution
//! \param batchSlots
//! \param batchSize current batch size
//! \param maxBatchSize maximum batch size
//! \param beamWidth beam width
//! \param maxSeqLen maximum sequence length
//! \param maxDraftTokens maximum number of draft tokens
//! \param stream stream
void invokeAcceptDraftTokensByIds(int32_t const* draftIds, int32_t const* targetIds, int32_t const* contextLengths,
    int32_t const* numsDraftTokens, int32_t* sequenceLengths, FinishedState const* finished,
    FinishedState* finishedFinal, int32_t* finishedSum, int32_t const* batchSlots, int32_t batchSize,
    int32_t maxBatchSize, int32_t beamWidth, int32_t maxSeqLen, int32_t maxDraftTokens, cudaStream_t stream);

//! \brief Performs probabilistic acceptance of draft tokens based on their probability distributions.
//! Corrects targetLogits for the next to the last accepted token
//! according to https://openreview.net/pdf?id=C9NEblP8vS
//!
//! \param draftLogits input/output buffer [draftTokens, batchSize, beamWidth, vocabSize].
//! Initially contains token logits of the draft model.
//! \param targetLogits input/output buffer [batchSize][draftTokens+1, beamWidth, vocabSize].
//! Vector of pointers to the logits.
//! Initially contains token logits of the target model.
//! It is modified in-place for next to the last accepted token such as
//! P'(x) = norm(max(0, P_{n+1}(x) - Q_{n+1}(x))), where N < maxDraftTokens is number of accepted tokens.
//! \param draftProbs output buffer [maxDraftTokens, batchSize, beamWidth, vocabSize].
//! Workspace buffer for token probabilities of the draft model.
//! \param targetProbs output buffer [maxDraftTokens+1, batchSize, beamWidth, vocabSize].
//! Workspace buffer for token probabilities of the target model.
//! \param numsDraftTokens input buffer [batchSize]. Number of draft tokens per request
//! \param finished output buffer [draftTokens, batchSize, beamWidth].
//! At each step sets to NOT_FINISHED if token is accepted or SKIP_DECODING if token is not accepted
//! \param curandState input buffer [batchSize]. Curand states properly
//! initialized using invokeCurandInitialize per request.
//! \param batchSlots
//! \param batchSize current batch size
//! \param maxBatchSize maximum batch size
//! \param beamWidth beam width
//! \param vocabSize unpadded vocab size
//! \param vocabSizePadded padded vocab size
//! \param maxDraftTokens maximum number of draft tokens
//! \param randomThreshold True if use uniformly sampled threshold for token acceptance
//! \param constantThreshold threshold used to accept tokens if randomThreshold is false
//! \param stream stream
template <typename T>
void acceptDraftTokensByLogits(T* draftLogits, T** targetLogits, T* draftProbs, T* targetProbs,
    int32_t const* numsDraftTokens, FinishedState* finished, curandState_t* curandState, int32_t const* batchSlots,
    int32_t batchSize, int32_t maxBatchSize, int32_t beamWidth, int32_t vocabSize, int32_t vocabSizePadded,
    int32_t maxDraftTokens, bool randomThreshold, float constantThreshold, cudaStream_t stream);

void invokeTransposeLogProbs(float* output_log_probs, float* output_log_probs_tiled, int32_t const* sequence_lengths,
    int32_t const* batchSlots, int32_t batch_size, int32_t max_batch_size, int32_t beam_width, int32_t max_seq_len,
    cudaStream_t stream);

//! \brief verifies draft medusa tokens given target tokens. Modifies outputIds tensor accordingly filling it with
//! accepted tokens. Fills logitsPtrs tensor with the pointers to the respective medusa logits tensor according
//! to the next after the last accepted token.
//!
//! \param outputIds input/output buffer [maxBatchSize, maxDraftSeqLen],
//! input tokens followed by draft tokens to be verified.
//! After accepting tokens, gets overwritten such that input tokens are followed by the accepted tokens
//! and one additional token -- next after the last accepted.
//! \param targetIds input buffer [maxBatchSize, maxTargetSeqLen], tokens predicted from the target medusa head
//! \param sequenceLengths input/output buffer [maxBatchSize], length of the data in outputIds without draft tokens
//! Incrememnted according to the accepted length
//! \param finishedFinal input buffer [maxBatchSize], finished states per request
//! \param batchSlots input buffer [batchSize], address map from local index
//! to global index [0, batchSize] -> [0, maxBatchSize]
//! \param paths input buffer [maxBatchSize, maxTokensPerStep, maxNumHeads+1],
//! paths to restore sequences from outputIds and targetIds. Should be filled with -1 for everything that is not path.
//! \param endIds input buffer [maxBatchSize], EOS ids per request
//! \param medusaLogits input buffer [maxNumHeads, maxBatchSize, maxTokensPerStep, vocabSize], pointer
//! to the logits from medusa heads
//! \param logitsPtrs output buffer [batchSize, maxNumHeads], contains pointers to the
//! respective rows of the medusaLogits for the next after the accepted token
//! \param batchSize current batch size
//! \param maxBatchSize maximum batch size
//! \param vocabSize vocab size
//! \param maxDraftSeqLen maximum sequence length of the sequence containing draft tokens
//! \param maxTargetSeqLen maximum sequence length predicted from target head
//! \param maxNumHeads maximum number of medusa heads
//! \param maxTokensPerStep maximum number of tokens per step configured in the system
//! \param stream stream
template <typename T>
void acceptDraftTokensByIdsWithPaths(runtime::TokenIdType* outputIds, runtime::TokenIdType const* targetIds,
    runtime::SizeType* sequenceLengths, FinishedState* finishedFinal, runtime::SizeType const* batchSlots,
    runtime::SizeType const* paths, runtime::TokenIdType const* endIds, T const* medusaLogits, T const** logitsPtrs,
    runtime::SizeType batchSize, runtime::SizeType maxBatchSize, runtime::SizeType vocabSize,
    runtime::SizeType maxDraftSeqLen, runtime::SizeType maxTargetSeqLen, runtime::SizeType maxNumHeads,
    runtime::SizeType maxTokensPerStep, cudaStream_t stream);
} // namespace kernels
} // namespace tensorrt_llm
