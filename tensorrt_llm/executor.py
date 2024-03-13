import asyncio
import time
from pathlib import Path
from queue import Queue
from typing import Any, Dict, Generator, List, Optional, Set, Tuple, Union

import torch
from janus import Queue as AsyncQueue
from transformers import AutoTokenizer

import tensorrt_llm.bindings as tllm
from tensorrt_llm._utils import mpi_broadcast, mpi_rank, mpi_world_size
from tensorrt_llm.hlapi.mpi_session import MpiSession, NodeSession, SocketClient
from tensorrt_llm.hlapi.tokenizer import TokenizerBase
from tensorrt_llm.hlapi.utils import GenerationOutput, print_traceback_on_error
from tensorrt_llm.logger import logger


def has_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class GenerationRequest:

    def __init__(self,
                 req_id: int,
                 ids: torch.Tensor,
                 end_id: int,
                 pad_id: int,
                 streaming: bool = True,
                 digit_input=False,
                 **kwargs):
        self.prompt = None
        self.ids = ids
        self.streaming = streaming
        self.kwargs = kwargs
        self.end_id = end_id
        self.pad_id = pad_id
        self.digit_input = digit_input
        self._id = req_id

    def get_inference_request(self) -> tllm.InferenceRequest:
        ir = tllm.InferenceRequest(self._id)
        ir.input_ids = self.ids.to(dtype=torch.int32)
        ir.is_streaming = self.streaming

        def set_property(name: str,
                         dtype: torch.dtype = torch.int32,
                         default: Any = None):
            if name in self.kwargs or default is not None:
                value = self.kwargs.get(name, default)
                setattr(ir, name, torch.tensor([value], dtype=dtype))

        set_property("max_new_tokens", default=[8])

        set_property("end_id", default=self.end_id)
        set_property("pad_id", default=self.pad_id)

        set_property("min_length")
        set_property("temperature", torch.float32)
        set_property("runtime_top_k", torch.float32)
        set_property("runtime_top_p", torch.float32)
        set_property("random_seed", torch.int64)

        return ir


class GenerationResult(GenerationOutput):

    def __init__(self,
                 generation_request: GenerationRequest,
                 tokenizer: Optional[TokenizerBase] = None) -> None:
        self.running = True
        self.done = False
        self.generation_request = generation_request
        self.tokenizer = tokenizer

        if has_event_loop():
            self._base_queue = AsyncQueue()
            self.queue = self._base_queue.sync_q
            self.aqueue = self._base_queue.async_q
        else:
            self._base_queue = Queue()
            self.queue = self._base_queue
            self.aqueue = None

        self.generation: Optional[torch.Tensor]
        if generation_request.streaming:
            self.generation = generation_request.ids
        else:
            self.generation = None

        # TODO: fill the following fields from GenerationOutput
        self.token_ids = []
        self.logprobs = []

    def enqueue(self, msg: Tuple[Union[str, Dict[str, torch.Tensor]], bool]):
        self.queue.put(msg)

    def handle_generation_msg(self, msg: Union[str, Dict[str, torch.Tensor]]):
        if isinstance(msg, str):
            raise RuntimeError(msg)

        # TODO[chunweiy]: Unify the msg format for parallel and non-parallel mode
        if isinstance(msg, dict):
            self.token_ids = msg["output_ids"][0][0]
        else:
            # this is for parallel mode
            assert isinstance(msg, list)
            self.token_ids = msg[0]

    @staticmethod
    def process_generation(msg: dict):
        token_ids = msg["output_ids"][0]
        # TODO: add other fields if needed
        return token_ids

    def wait_step(self, timeout: Optional[float] = None):
        msg, self.done = self.queue.get(timeout=timeout)
        self.handle_generation_msg(msg)

    async def await_step(self):
        assert self.aqueue is not None
        msg, self.done = await self.aqueue.get()
        self.handle_generation_msg(msg)

    @property
    def text(self) -> str:
        if self.tokenizer is None:
            return ''
        return self.tokenizer.decode(self.token_ids)

    def result(self, timeout: Optional[float] = None) -> "GenerationResult":
        while not self.done:
            self.wait_step(timeout)
        return self

    async def aresult(self) -> "GenerationResult":
        while not self.done:
            await self.await_step()
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self.done:
            raise StopIteration

        self.wait_step()
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.done:
            raise StopAsyncIteration

        await self.await_step()
        return self


class GenerationExecutor:
    TERMINATE_REQUEST_ID = 0

    def __init__(
        self,
        engine_dir: Path,
        tokenizer: Union[str, Path, TokenizerBase, None],
        max_beam_width: int = 1,
        executor_type: tllm.TrtGptModelType = tllm.TrtGptModelType.
        InflightBatching,
        executor_policy: tllm.SchedulerPolicy = tllm.SchedulerPolicy.
        GUARANTEED_NO_EVICT,
        executor_config: tllm.TrtGptModelOptionalParams = tllm.
        TrtGptModelOptionalParams(),
    ) -> None:

        self.active_requests = 0

        self.tokenizer = tokenizer
        if tokenizer is not None and not isinstance(tokenizer, TokenizerBase):
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer,
                legacy=False,
                padding_side='left',
                truncation_side='left',
                trust_remote_code=True,
                use_fast=True)

        # NOTE: underscore variables are used for communication with the C++ runtime
        self._requests: List[tllm.InferenceRequest] = []
        self._results: Dict[int, GenerationResult] = {}
        self._cancelled_ids: Set[int] = set()
        self._completed: Queue = Queue()
        if has_event_loop():
            self._stats = AsyncQueue()
            self.stats_queue = self._stats.sync_q
            self.stats_aqueue = self._stats.async_q
        else:
            self._stats = Queue()
            self.stats_queue = self._stats
            self.stats_aqueue = None

        self.engine = tllm.GptManager(engine_dir, executor_type, max_beam_width,
                                      executor_policy, self.fetch_requests,
                                      self.handle_response,
                                      self.get_cancelled_ids, self.handle_stats,
                                      executor_config,
                                      GenerationExecutor.TERMINATE_REQUEST_ID)

        self._next_request_id = GenerationExecutor.TERMINATE_REQUEST_ID + 1

    def submit(self, request: GenerationRequest) -> GenerationResult:
        """
            Low-level API to the executor. Return a "future" GenerationResult which can be waited.
        """

        inference_request = request.get_inference_request()

        tokenizer = self.tokenizer if not request.digit_input else None
        result = GenerationResult(request, tokenizer)
        self._results[inference_request.request_id] = result

        self.active_requests += 1
        self._requests.append(inference_request)

        return result

    def get_next_request_id(self) -> int:
        # underlying type is uint64
        uint64_max = 2**64 - 1
        request_id = self._next_request_id
        self._next_request_id = (request_id + 1) % uint64_max
        return request_id

    def generate_async(
            self,
            prompt: Union[str, List[int], List[str], List[List[int]]],
            streaming: bool,
            max_new_tokens: Union[int, List[int]],
            end_id: int = -1,
            pad_id: int = -1
    ) -> Union[GenerationResult, List[GenerationResult]]:
        batched = False
        digit_input = False
        if isinstance(prompt, list):
            if isinstance(prompt[0], str):  # List[str]
                batched = True
                if isinstance(max_new_tokens, int):
                    max_new_tokens = [max_new_tokens] * len(prompt)
            elif isinstance(prompt[0], int):  # List[int]
                digit_input = True
                prompt = [prompt]
                if not isinstance(max_new_tokens, list):
                    max_new_tokens = [max_new_tokens]
            # List[List[int]]
            elif isinstance(prompt[0], list) and isinstance(prompt[0][0], int):
                batched = True
                digit_input = True
                if not isinstance(max_new_tokens, list):
                    max_new_tokens = [max_new_tokens] * len(prompt)
        else:  # str
            prompt = [prompt]
            if not isinstance(max_new_tokens, list):
                max_new_tokens = [max_new_tokens]

        def get_ids(prompt: str | List[int]) -> torch.Tensor:
            if digit_input:
                return torch.tensor([prompt], dtype=torch.int32)
            return self.tokenizer.encode(prompt,
                                         return_tensors="pt",
                                         return_attention_mask=False)

        if end_id == -1:
            assert self.tokenizer is not None, "Please specify end_id if tokenizer is not provided"
            end_id = self.tokenizer.eos_token_id
            pad_id = getattr(self.tokenizer, "pad_token_id", end_id)

        results = [
            self.submit(
                GenerationRequest(req_id=self.get_next_request_id(),
                                  ids=get_ids(p),
                                  streaming=streaming,
                                  max_new_tokens=[m],
                                  pad_id=pad_id,
                                  end_id=end_id,
                                  digit_input=digit_input))
            for p, m in zip(prompt, max_new_tokens)
        ]
        if not batched:
            results = results[0]
        return results

    def generate(
            self,
            prompt: Union[str, List[str]],
            max_new_tokens: Union[int, List[int]],
            end_id: int = -1,
            pad_id: int = -1
    ) -> Union[GenerationResult, List[GenerationResult]]:
        results = self.generate_async(prompt,
                                      False,
                                      max_new_tokens,
                                      end_id=end_id,
                                      pad_id=pad_id)
        result_list = [results] if isinstance(results,
                                              GenerationRequest) else results
        for result in result_list:
            result.result()
        return results

    def get_stats(self):
        return self.stats_queue.get()

    async def aget_stats(self):
        assert self.stats_aqueue is not None
        return await self.stats_aqueue.get()

    def wait_first_completed(
        self, futures: List[GenerationResult]
    ) -> Generator[GenerationResult, None, None]:
        wait_set = set(f.generation_request._id for f in futures)

        # clear already-finished requests
        for f in futures:
            if f.done:
                wait_set.remove(f.generation_request._id)
                yield f

        # wait remaining active requests
        while len(wait_set) > 0:
            req_id = self._completed.get()
            if req_id in wait_set:
                wait_set.remove(req_id)
                yield self._results[req_id]

    # Callbacks for BatchManager
    def fetch_requests(self, max_num_sequences) -> List[tllm.InferenceRequest]:
        fetched = []
        for _ in range(max_num_sequences):
            if len(self._requests) == 0:
                break
            fetched.append(self._requests.pop())
        return fetched

    def handle_response(self, req_id: int, tensors: List[tllm.NamedTensor],
                        finished: bool, err: str) -> None:
        self._results[req_id].enqueue(
            ({t.name: t.tensor
              for t in tensors
              if t.tensor is not None} if not err else err, finished))
        if finished:
            self._completed.put(req_id)

    def get_cancelled_ids(self) -> Set[int]:
        return self._cancelled_ids

    def handle_stats(self, stats: str):
        while self.stats_queue.full():
            self.stats_queue.get()

        self.stats_queue.put(stats)

    def __enter__(self):
        self.engine.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.engine is not None:
            self.engine.__exit__(exc_type, exc_value, traceback)
            self.engine = None

    def __del__(self):
        self.__exit__(None, None, None)


class ParallelGenerationExecutor(GenerationExecutor):
    ''' GenerationExecutor with MPI enabled. '''

    def __init__(
        self,
        world_size: int,
        engine_dir: Path,
        tokenizer: Union[str, Path, TokenizerBase, None],
        max_beam_width: int = 1,
        executor_type: tllm.TrtGptModelType = tllm.TrtGptModelType.
        InflightFusedBatching,
        executor_policy: tllm.SchedulerPolicy = tllm.SchedulerPolicy.
        GUARANTEED_NO_EVICT,
        executor_config: tllm.TrtGptModelOptionalParams = tllm.
        TrtGptModelOptionalParams(),
        socket_client: Optional[SocketClient] = None,
    ) -> None:

        self.on_PMP = mpi_world_size() == 1
        self.on_MPI = mpi_world_size() > 1

        self._terminated = False
        self._terminated_sync = False

        self.active_requests = 0

        self.tokenizer = tokenizer
        if tokenizer is not None and not isinstance(tokenizer, TokenizerBase):
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer,
                legacy=False,
                padding_side='left',
                truncation_side='left',
                trust_remote_code=True,
                use_fast=True)

        # NOTE: underscore variables are used for communication with the C++ runtime
        self._requests: list[tllm.InferenceRequest] = []
        self._results: dict[int, GenerationResult] = {}
        self._cancelled_ids: set[int] = set()
        self._completed: Queue = Queue()
        if has_event_loop():
            self._stats = AsyncQueue()
            self.stats_queue = self._stats.sync_q
            self.stats_aqueue = self._stats.async_q
        else:
            self._stats = Queue()
            self.stats_queue = self._stats
            self.stats_aqueue = None

        self._next_request_id = GenerationExecutor.TERMINATE_REQUEST_ID + 1
        self.socket_client = socket_client

        if self.on_PMP:
            # initialize the executor on each MPI node
            assert isinstance(self.tokenizer,
                              TokenizerBase), "tokenizer not initialized"

            self.mpi_session = MpiSession(
                n_workers=world_size,
                async_callback=self._async_listener_callback)
            self.socket_client = self.mpi_session.get_socket_client()

            self.mpi_session.submit_sync(
                ParallelGenerationExecutor._node_init_executor_task, engine_dir,
                self.tokenizer, max_beam_width, executor_type, executor_policy,
                executor_config, self.socket_client)
        else:
            self.engine = tllm.GptManager(
                engine_dir, executor_type, max_beam_width, executor_policy,
                self.fetch_requests_on_mpi_node,
                self.handle_response_on_mpi_node, self.get_cancelled_ids,
                self.handle_stats, executor_config,
                GenerationExecutor.TERMINATE_REQUEST_ID)

    def submit(self, request: GenerationRequest) -> GenerationResult:
        # submit on the PMP
        inference_request = request.get_inference_request()
        result = GenerationResult(request, self.tokenizer)
        self._results[inference_request.request_id] = result

        self.active_requests += 1

        self.mpi_session.submit_sync(
            ParallelGenerationExecutor._node_add_request_task,
            inference_request)

        return result

    @print_traceback_on_error
    @staticmethod
    def _node_add_request_task(inference_request):
        executor: GenerationExecutor = NodeSession.state
        assert isinstance(executor,
                          GenerationExecutor), 'executor not initialized'
        executor._requests.append(inference_request)

    @print_traceback_on_error
    @staticmethod
    def _node_init_executor_task(
        engine_dir: Path,
        tokenizer: TokenizerBase,
        max_beam_width: int,
        executor_type: tllm.TrtGptModelType,
        executor_policy: tllm.SchedulerPolicy,
        executor_config: tllm.TrtGptModelOptionalParams,
        socket_client: Optional[SocketClient],
    ):
        ''' Create a local GenerationExecutor instance for each MPI process. '''
        assert not NodeSession.is_initialized(), 'executor already initialized'

        logger.info(f'Initializing executor on MPI node #{mpi_rank()}')

        world_size = mpi_world_size()
        NodeSession.state = ParallelGenerationExecutor(
            world_size,
            engine_dir,
            tokenizer,
            max_beam_width,
            executor_type,
            executor_policy,
            executor_config=executor_config,
            socket_client=socket_client)

    # Callbacks for BatchManager

    @print_traceback_on_error
    def fetch_requests_on_mpi_node(
            self, max_num_sequences) -> List[tllm.InferenceRequest]:
        if mpi_rank() != 0 or self._terminated_sync:
            if self._terminated:
                return []

        terminated = mpi_broadcast(self._terminated, 0)
        if terminated:
            logger.warning(f'#node{mpi_rank()} to terminate')
            self._terminated_sync = True
            self._terminated = True

        if terminated:
            return []

        batch_size = 0
        fetched = []
        if mpi_rank() == 0:
            batch_size = min(len(self._requests), max_num_sequences)
        batch_size = mpi_broadcast(batch_size, 0)

        for _ in range(batch_size):
            # the MPIPoolExecutor will always submit the same input to every worker, sometimes they arrive at slightly different time
            while len(self._requests) == 0:
                time.sleep(0.05)
            fetched.append(self._requests.pop())

        return fetched

    def handle_response_on_mpi_node(self, req_id: int,
                                    tensors: List[tllm.NamedTensor],
                                    finished: bool, err: str) -> None:
        if mpi_rank() != 0:
            return

        tensor_dic = {t.name: t.tensor for t in tensors if t.tensor is not None}
        output = GenerationResult.process_generation(
            tensor_dic) if not err else err

        self.socket_client.send(
            dict(
                req_id=req_id,
                output=output if isinstance(output, str) else output.tolist(),
                finished=finished,
            ))

    def _async_listener_callback(self, data: Dict[str, Any]):
        req_id = data['req_id']
        output = data['output']
        finished = data['finished']
        self._results[req_id].enqueue((output, finished))
        if finished:
            self._completed.put(req_id)

    @print_traceback_on_error
    @staticmethod
    def _node_quit_task():
        executor: GenerationExecutor = NodeSession.state
        assert isinstance(executor,
                          GenerationExecutor), 'executor not initialized'
        if mpi_rank() == 0:
            executor._terminated = True

        time.sleep(1)
        executor.engine.__exit__(None, None, None)
        NodeSession.state = None

    def _shutdown_mpi_nodes(self):
        self.mpi_session.submit_sync(ParallelGenerationExecutor._node_quit_task)

    def shutdown(self):
        if self.on_PMP and self.mpi_session is not None:
            self._shutdown_mpi_nodes()
            self.mpi_session.shutdown()
            self.mpi_session = None

    def __del__(self):
        self.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
        return False
