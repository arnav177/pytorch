# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]
import copy
import os
import sys
import tempfile
import unittest
from typing import Dict, List, Optional, Tuple

from model_registry import ModelWithKwargs, MultiMLP

import torch
import torch.distributed as dist
from torch.distributed.pipelining import (
    ManualPipelineStage,
    pipeline,
    PipelineStage,
    Schedule1F1B,
    ScheduleGPipe,
    ScheduleInterleaved1F1B,
    ScheduleLoopedBFS,
)
from torch.distributed.pipelining.PipelineScheule import _ComputationType
from torch.testing._internal.common_cuda import TEST_MULTIGPU
from torch.testing._internal.common_distributed import (
    MultiProcContinousTest,
    requires_nccl,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    skip_but_pass_in_sandcastle_if,
)


d_hid = 512
batch_size = 256

torch.manual_seed(0)

class MockPipelineStageBase(_PipelineStageBase):
    def __init__(self, *args, **kwargs):
        # Mock the necessary attributes
        self.group_size = kwargs.get('group_size', 1)
        self.group_rank = kwargs.get('group_rank', 0)
        self.group = kwargs.get('group', None)

class ScheduleTest(MultiProcContinousTest):
    @classmethod
    def backend_str(cls) -> str:
        # Testing with NCCL backend
        return "nccl"

    @classmethod
    def setUpClass(cls):
        """
        Class-scope test fixture. Run once for entire test class, before any test starts.
        Set up the device.
        """
        super().setUpClass()
        dev_id = cls.rank % torch.cuda.device_count()
        cls.device = torch.device(f"cuda:{dev_id}")

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    def test_kwargs_with_tracer(self, ScheduleClass):
        mod = ModelWithKwargs(d_hid)
        mod.to(self.device)

        x = torch.randn(batch_size, d_hid, device=self.device)
        y = torch.randn(batch_size, d_hid, device=self.device)
        target = torch.randn(batch_size, d_hid, device=self.device)
        loss_fn = torch.nn.MSELoss(reduction="sum")

        chunks = 4
        pipe = pipeline(
            mod,
            chunks,
            example_args=(x,),
            example_kwargs={"y": y},
        )

        stage = PipelineStage(
            pipe,
            self.rank,
            device=self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        if self.rank == 0:
            schedule.step(x, y=y)
        elif self.rank == self.world_size - 1:
            losses = []
            out = schedule.step(target=target, losses=losses)
        else:
            schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            ref_out = mod(x, y=y)
            ref_loss = loss_fn(ref_out, target)
            pipe_loss = sum(losses)
            torch.testing.assert_close(out, ref_out, rtol=1e-2, atol=5e-3)
            torch.testing.assert_close(pipe_loss, ref_loss)

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    @parametrize("ModelClass", [MultiMLP])
    def test_grad_with_tracer(self, ScheduleClass, ModelClass):
        mod = ModelClass(d_hid)
        mod.to(self.device)

        ref_mod = copy.deepcopy(mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Create a pipeline
        chunks = 4
        split_spec = mod.split_spec if hasattr(mod, "split_spec") else None
        pipe = pipeline(
            mod,
            chunks,
            example_args=(x,),
            split_spec=split_spec,
        )

        stage = PipelineStage(
            pipe,
            self.rank,
            device=self.device,
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        stage_module = pipe.get_stage_module(self.rank)
        for _ in range(2):
            # Zero gradients
            stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for name, p in stage_module.named_parameters():
            ref_p = ref_mod.get_parameter(name)
            try:
                torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
            except AssertionError:
                print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleGPipe, Schedule1F1B])
    def test_grad_with_manual(self, ScheduleClass):
        full_mod = MultiMLP(d_hid, n_layers=self.world_size)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        submod_name = f"layers.{self.rank}"
        stage_module = full_mod.get_submodule(submod_name)
        chunks = 4
        # Create a pipeline stage to wrap that submodule
        stage = ManualPipelineStage(
            stage_module,
            self.rank,
            self.world_size,
            self.device,
            chunks,
            input_args=x.chunk(chunks)[0],
        )

        # Attach to a schedule
        schedule = ScheduleClass(stage, chunks, loss_fn=loss_fn)

        # Run
        for _ in range(2):
            # Zero gradients
            stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        ref_submod = ref_mod.get_submodule(submod_name)
        for name, p in stage_module.named_parameters():
            ref_p = ref_submod.get_parameter(name)
            try:
                torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
            except AssertionError:
                print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                raise

    @requires_nccl()
    @skip_but_pass_in_sandcastle_if(not TEST_MULTIGPU, "NCCL test requires 2+ GPUs")
    @parametrize("ScheduleClass", [ScheduleInterleaved1F1B, ScheduleLoopedBFS])
    def test_grad_with_manual_interleaved(self, ScheduleClass):
        stages_per_rank = 2
        n_stages = stages_per_rank * self.world_size
        full_mod = MultiMLP(d_hid, n_layers=n_stages)
        full_mod.to(self.device)

        ref_mod = copy.deepcopy(full_mod)
        x = torch.randn(batch_size, d_hid, device=self.device)
        with torch.no_grad():
            y = ref_mod(x)
            # Add a small perturbation
            target = y + torch.randn(batch_size, d_hid, device=self.device)

        loss_fn = torch.nn.MSELoss(reduction="sum")

        # Run reference
        for _ in range(2):
            ref_mod.zero_grad()
            ref_out = ref_mod(x)
            ref_loss = loss_fn(ref_out, target)
            ref_loss.backward()

        # Get a submodule, e.g. `layers.0` or `layers.1`
        stage_indices = [
            self.rank + i * self.world_size for i in range(stages_per_rank)
        ]
        print(f"Rank {self.rank} stages: {stage_indices}")
        submod_names = [f"layers.{i}" for i in stage_indices]
        stage_modules = [
            full_mod.get_submodule(submod_name) for submod_name in submod_names
        ]
        # Create a pipeline stage to wrap that submodule
        chunks = 8
        input_args = x.chunk(chunks)[0]
        stages = [
            ManualPipelineStage(
                stage_module,
                stage_idx,
                n_stages,
                self.device,
                chunks,
                input_args=input_args,
            )
            for stage_module, stage_idx in zip(stage_modules, stage_indices)
        ]

        # Attach to a schedule
        schedule = ScheduleClass(stages, chunks, loss_fn=loss_fn)

        # Run
        for _ in range(2):
            # Zero gradients
            for stage_module in stage_modules:
                stage_module.zero_grad()
            if self.rank == 0:
                schedule.step(x)
            elif self.rank == self.world_size - 1:
                losses = []
                out = schedule.step(target=target, losses=losses)
            else:
                schedule.step()

        dist.barrier()

        # Last rank checks result
        if self.rank == self.world_size - 1:
            # Check output
            torch.testing.assert_close(out, ref_out)
            # Check loss
            # Since the reduction used in the loss function above is "sum", we use
            # "sum" here to reduce microbatch losses into a single value too.
            pipe_loss = sum(losses)
            torch.testing.assert_close(pipe_loss, ref_loss)

        # Every rank checks gradients
        for stage_module, submod_name in zip(stage_modules, submod_names):
            # Get corresponding submodule from reference model
            ref_submod = ref_mod.get_submodule(submod_name)
            # Check gradients per parameter
            for name, p in stage_module.named_parameters():
                ref_p = ref_submod.get_parameter(name)
                try:
                    torch.testing.assert_close(p.grad, ref_p.grad, rtol=1e-5, atol=4e-5)
                except AssertionError:
                    print(f"Gradient test failed for {name}: {p.grad} vs {ref_p.grad}")
                    raise


instantiate_parametrized_tests(ScheduleTest)


class TestSchedulePlan(unittest.TestCase):
    def _validate_pipeline_order(
        self,
        pipeline_order: List[List[Optional[Tuple[_ComputationType, int, int]]]],
        num_microbatches: int,
    ):
        """
        pipeline_order[rank] = [(computation_type, microbatch_index, stage_index), ...]

        Validating that the pipeline order follows the rules:
        1. Forward action for a microbatch must be before the Backward action for that microbatch
        2. Recv for a microbatch must be before the send for that microbatch
        3. Microbatch index is handled in sequential order for each stage
        4. A later stage cannot operate on a microbatch before any of the previous stages have operated on it
        5. Same microbatch cannot be handled in the same time step across ranks
        """
        # microbatch_index: (current computation type, current stage)
        error_msg = []
        microbatch_process_info: Dict[int, Tuple(_ComputationType, int)] = {}
        max_timestep = max(len(rank_list) for rank_list in pipeline_order)
        for timestep in range(max_timestep):
            error_msg = None
            current_timestep_actions = []
            for rank in range(len(pipeline_order)):
                action = pipeline_order[rank][timestep]
                if action is not None:
                    current_timestep_actions.append(action)

            if len(current_timestep_actions) == 0:
                error_msg.append(
                    "All actions were None, there is an unnecessary gap in the schedule"
                )

            # Ensure that no microbatch is operated on twice in current_timestep_actions
            unique_microbatch_indices = {
                action[1] for action in current_timestep_actions
            }
            if len(unique_microbatch_indices) != len(current_timestep_actions):
                error_msg.append(
                    "Duplicate microbatch index found in current_timestep_actions"
                )

            # Add additional checks for other rules here...
            for action in current_timestep_actions:
                computation_type, mb_index, stage_index = action
                # first microbatch
                if mb_index not in microbatch_process_info:
                    if computation_type != _ComputationType.FORWARD or stage_index != 0:
                        error_msg.append(f"Incorrect start for microbatch {mb_index}")
                    microbatch_process_info[mb_index] = (computation_type, stage_index)
                else:
                    # if the microbatch is included, check that the current stage is right after prev
                    prev_computation, prev_stage = microbatch_process_info[mb_index]
                    if prev_computation == _ComputationType.FORWARD:
                        if prev_stage == num_microbatches - 1:
                            expected_stage = num_microbatches - 1
                            expected_computation = _ComputationType.BACKWARD
                        else:
                            expected_stage = prev_stage + 1
                            expected_computation = _ComputationType.FORWARD
                    elif prev_computation == _ComputationType.BACKWARD:
                        if prev_stage == 0:
                            error_msg.append(
                                f"[{mb_index=}] already finished backward computation"
                            )
                            expected_stage = None
                            expected_computation = None
                        else:
                            expected_stage = prev_stage - 1
                            expected_computation = _ComputationType.BACKWARD
                    else:
                        raise ValueError(
                            f"Computation type {prev_computation} not supported"
                        )

                    if expected_stage != stage_index:
                        error_msg.append(
                            f"[{mb_index=}] {expected_computation=}, actual computation {computation_type}",
                            f"| {expected_stage=}, actual stage {stage_index}",
                        )

            if len(error_msg) != 0:
                self.fail(f"Error at timestep {timestep}" + ",".join(error_msg))

    def test_pipeline_order(self):
        # always assume rank 0
        rank = 0

        # happy paths
        num_stages = 2
        num_microbatches = 8
        group_size = 4
        stages = [
            MockPipelineStageBase(group_size=group_size, group_rank=rank)
            for i in range(num_stages)
        ]

        schedule = ScheduleInterleaved1F1B(stages, num_microbatches)
        self._validate_pipeline_order(schedule.pipeline_order)

        # sad paths (should fail)

        print("finished test_pipeline_order")
        pass


if __name__ == "__main__":
    # Run only the TestSchedulePlan tests (single process)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestSchedulePlan)
    runner = unittest.TextTestRunner()
    runner.run(suite)

    # Check if GPU and NCCL are available
    if not (
        dist.is_available()
        and dist.is_nccl_available()
        and torch.cuda.device_count() > 1
    ):
        print(
            "c10d NCCL not available or not enough GPUs, skipping tests",
            file=sys.stderr,
        )
        sys.exit(0)

    rank = int(os.getenv("RANK", -1))
    world_size = int(os.getenv("WORLD_SIZE", 2))

    if rank != -1:
        # Launched with torchrun or other multi-proc launchers. Directly run the test.
        ScheduleTest.run_rank(rank, world_size)
    else:
        # Launched as a single process. Spawn subprocess to run the tests.
        # Also need a rendezvous file for `init_process_group` purpose.
        rdvz_file = tempfile.NamedTemporaryFile(delete=False).name
        torch.multiprocessing.spawn(
            ScheduleTest.run_rank,
            nprocs=world_size,
            args=(world_size, rdvz_file),
        )

    # test the non multiprocessing tests
    # unittest.main()
