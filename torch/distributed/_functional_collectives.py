import warnings

import weakref
from typing import Any, cast, List, Tuple, Union

import sys
import torch
import torch.distributed as dist

import torch.distributed.distributed_c10d as c10d

from torch._C import _disabled_torch_function_impl
from torch.utils._pytree import tree_map

"""
New traceable, functional collectives.
RFC: https://github.com/pytorch/pytorch/issues/93173

  compiler: trace these ops with plain-old-data schemas, then choose how to lower them.
  eager: execute these 'functional' ops which in eager return AsyncCollectiveTensor subclasses,
         automatically calling .wait() on underlying/hidden async 'work' obj only when fed to
         a downstream op.

Issues:
* Where should these ops live? Couldn't `import torch` if putting these ops in existing torch.distributed files
* Proper support for eager requires inplace ops. We should explore having it as an option for the API.
"""

"""
Functional collectives are asynchronous only and we perform implicit stream synchronization
on behalf of the user.

We use AsyncCollectiveTensor to wrap the result tensor of a collective and it lets us witness
first usage of the tensor and insert cross stream sync at the right place.

The above are the easy bits, the hard one is how we match the Work object returned by
c10d and the tensor AsyncCollectiveTensor wraps. We alloc the tensor inside the collective
op implementation (see ``clone()`` call in ``_all_reduce``) and then it's handled by the
dispatcher which might call other implementations that are allowed to change the returned
tensor - even return a tensor with a different shape (see ``torch.vmap``).

This means the caller of our ops receives a Tensor that is not guaranteed to be the same
allocated by our implementations and that makes pairing The AsyncTensor to the original
tensor a lot harder. This pairing is needed so we can lookup the Work object to use.

Originally, we tried WeakKeyDictionary to map from Tensor to Work, but because Tensor's
identity is not stable across dispatch, the op caller would end up with a different Tensor
instance that would not match any in the dictionary.

With Tensor identity out of the question, we decided use the tensor data pointer, which
should be stable across all the Tensor changes done during dispatch.

We have a dictionary of tensor::data_ptr -> Work that we insert right after we call into c10d.

We use this dictionary when AsyncCollectiveTensor is used to invoke Work::wait()

Finally, we setup a finalizer against the tensor wrapper to observe it getting collected so we
can clean up stale entries in the dictionary.

To eliminate the possiblity of races we have a global version counter that is used by the finalizer.

As a wise man said once: Don't cross the streams (https://www.youtube.com/watch?v=wyKQe_i9yyo)

"""
data_ptr_to_work = dict()
work_version = 0

def _register_tensor_work(tensor, work):
    # Note: called directly by inductor codegen currently
    global data_ptr_to_work
    global work_version
    data_ptr_to_work[tensor.data_ptr()] = (work_version, work)
    work_version += 1

def _wait_and_clear_tensor(data_ptr, version):
    global data_ptr_to_work
    version_and_work = data_ptr_to_work.get(data_ptr)

    if version_and_work is not None and version_and_work[0] == version:
        version_and_work[1].wait()
        del data_ptr_to_work[data_ptr]

def _register_wrapper_tensor(tensor_wrapper, tensor):
    global data_ptr_to_work
    version, _ = data_ptr_to_work.get(tensor.data_ptr(), (None, None))
    if version is None:
        warnings.warn(
            "Trying to register finalizers to AsyncCollectiveTensor but the inner tensor is already gone"
        )
    else:
        # We force the collective to be waited in the case this tensor goes away to reduce the change of deadlocks.
        weakref.finalize(tensor_wrapper, _wait_and_clear_tensor, tensor.data_ptr(), version)

def _wait_tensor(tensor: torch.Tensor) -> torch.Tensor:
    global data_ptr_to_work
    data_ptr = tensor.data_ptr()
    version_and_work = data_ptr_to_work.get(data_ptr)
    if version_and_work is not None:
        _wait_and_clear_tensor(data_ptr, version_and_work[0])
    return tensor


class AsyncCollectiveTensor(torch.Tensor):
    r"""
    A Tensor subclass that is only used in eager mode, to hold a 'work' object
    and then wait on it before invoking a real op.

    Usage, from inside functional collective:
    def functional_collective(input):
        input = input.clone()
        mutated_input, work = c10d.{inplace_collective}(input)
        return AsyncCollectiveTensor(mutated_input, work)
    """
    _tensor: torch.Tensor

    __torch_function__ = _disabled_torch_function_impl

    @staticmethod
    def __new__(cls, tensor: torch.Tensor):
        t = tensor
        r = torch.Tensor._make_subclass(cls, t, require_grad=t.requires_grad)
        r._tensor = tensor  # type: ignore[attr-defined]
        return r

    def __repr__(self):
        return f"AsyncCollectiveTensor({self._tensor})"

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        def unwrap(e: Any):
            if isinstance(e, AsyncCollectiveTensor):
                return wait_tensor(e._tensor)
            return e

        unwrapped_args = tree_map(unwrap, args)
        unwrapped_kwargs = tree_map(unwrap, kwargs)

        out = func(*unwrapped_args, **unwrapped_kwargs)
        return out

def _str_to_reduce_op(reduceOp: str) -> dist.ReduceOp:
    reduceOp = reduceOp.upper()
    op = dist.ReduceOp.RedOpType.__members__.get(reduceOp)
    if op is None:
        raise ValueError(f"Invalid reduce operation {reduceOp}")
    return cast(dist.ReduceOp, op)

# TODO assert if ranks has duplicated entries
def _all_reduce(self, reduceOp, tag, ranks, group_size):
    op = _str_to_reduce_op(reduceOp)
    group = c10d._find_or_create_pg_by_ranks_and_tag(tag, ranks, group_size)
    assert group is not None

    inplace_tensor = self.clone(memory_format=torch.contiguous_format)
    work = dist.all_reduce(inplace_tensor, op=op, group=group, async_op=True)
    _register_tensor_work(inplace_tensor, work)

    return inplace_tensor

def _all_gather_into_tensor(shard, tag, ranks, group_size):
    # TODO add dim support?
    group = c10d._find_or_create_pg_by_ranks_and_tag(tag, ranks, group_size)
    assert group is not None
    out_size = list(shard.size())
    out_size[0] *= group_size
    out_tensor = shard.new_empty(out_size)
    assert out_tensor.is_contiguous()
    # FIXME gloo doesn't support _allgather_base
    if shard.is_cpu:
        tensor_list = list(torch.chunk(out_tensor, group_size))
        work = dist.all_gather(tensor_list, shard, group=group, async_op=True)
    else:
        work = dist.all_gather_into_tensor(out_tensor, shard, group=group, async_op=True)
    _register_tensor_work(out_tensor, work)

    return out_tensor

def _reduce_scatter_tensor(
    input: torch.Tensor,
    reduceOp: str,
    scatter_dim: int,
    tag: str,
    ranks: List[int],
    group_size: int,
):
    # TODO add dim support?
    assert scatter_dim == 0, "Only scatter_dim = 0 is supported for now."
    group = c10d._find_or_create_pg_by_ranks_and_tag(tag, ranks, group_size)
    assert group is not None
    op = _str_to_reduce_op(reduceOp)
    out_size = list(input.size())
    out_size[scatter_dim] //= group_size
    out_tensor = input.new_empty(out_size)
    work = dist.reduce_scatter_tensor(
        out_tensor, input, op=op, group=group, async_op=True
    )
    _register_tensor_work(out_tensor, work)

    return out_tensor


RANK_TYPES = Union[List[int], List[List[int]], dist.ProcessGroup, "dist._tensor.DeviceMesh", Tuple["dist._tensor.DeviceMesh", int]]

def _expand_group(group: RANK_TYPES, tag: str = "") -> Tuple[str, List[int], int]:
    # Cannot import on the top level to avoid circular imports
    import torch.distributed._tensor as dt
    rankset: List[int]
    if isinstance(group, list):
        if isinstance(group[0], list):
            nested_list = cast(List[List[int]], group)
            rankset = []
            group_size = -1
            for rs in nested_list:
                rankset.extend(rs)
                if group_size != -1 and group_size != len(rs):
                    raise ValueError(
                        f"group sizes must be identical found {group_size} and {len(rs)}"
                    )
                group_size = len(rs)
        else:
            rankset = cast(List[int], group)
            group_size = len(rankset)
    elif isinstance(group, dist.ProcessGroup):
        rankset = dist.get_process_group_ranks(group)
        group_size = len(rankset)
        tag = tag or c10d._get_group_tag(group)
    elif isinstance(group, dt.DeviceMesh):
        rankset = group.mesh.flatten().tolist()
        group_size = group.mesh.size(0)
        rankset = group.mesh.swapdims(-1, 0).reshape(-1, group_size).flatten().tolist()
        tag = tag or c10d._get_group_tag(group.get_dim_groups()[0])
    elif isinstance(group, tuple):
        if len(group) == 2 and isinstance(group[0], dt.DeviceMesh) and isinstance(group[1], int):
            dmesh = group[0]
            dim = group[1]
            group_size = dmesh.mesh.size(dim)
            rankset = dmesh.mesh.swapdims(-1, dim).reshape(-1, group_size).flatten().tolist()
            tag = tag or c10d._get_group_tag(dmesh.get_dim_groups()[dim])
        else:
            raise ValueError("Invalid tuple for group must be (DeviceMesh, int)")
    else:
        raise ValueError("Invalid type for group, must be one of List, Processgroup, DeviceMesh or (DeviceMesh, int).")

    return (tag, rankset, group_size)


def wait_tensor(tensor):
    """
    Wait on a tensor returned by the collectives ops.

    Waiting follows device semantics, which means blocking on CPU and synchronizing streams on CUDA.
    """
    return torch._C._nn.wait_tensor(tensor)  # type: ignore[attr-defined]


def all_reduce(self: torch.Tensor, reduceOp: str, group: RANK_TYPES, tag: str = ""):
    """
    Reduces the tensor data across all machines in such a way that all get
    the final result.

    The input tensor is left unmodified.

    Group can be one of:
        List[int]: ranks participating in the collective.
        List[List[int]]: 2D mesh of ranks taking part of this collective in MPMD.
        ProcessGroup: Will perform a collective using the ranks and tag of the PG.
        DeviceMesh: Do a SPMD collective over all ranks of the mesh
        (DeviceMesh, int): Do a MPMD collective over one dimension of the DeviceMesh

    :: N.B. If you pass a PG or a 1D list to perform a MPMD collective, the compiler won't be able to recover
    that information and perform collective algebraic optimization. Use other forms of input for that.
    """
    tag, rankset, group_size = _expand_group(group, tag)
    tensor = torch._C._nn.all_reduce(self, reduceOp, tag, rankset, group_size)  # type: ignore[attr-defined]
    res = AsyncCollectiveTensor(tensor)
    _register_wrapper_tensor(res, tensor)
    return res

def all_gather_tensor(
    self: torch.Tensor,
    gather_dim: int,
    group: RANK_TYPES,
    tag: str = "",
):
    """
    Gather tensor data across from all machines and concatenate over ``gather_dim``.

    Note that it currently only supports gather_dim = 0.

    The input tensor is left unmodified.
    Group can be one of:
        List[int]: ranks participating in the collective.
        List[List[int]]: 2D mesh of ranks taking part of this collective in MPMD.
        ProcessGroup: Will perform a collective using the ranks and tag of the PG.
        DeviceMesh: Do a SPMD collective over all ranks of the mesh
        (DeviceMesh, int): Do a MPMD collective over one dimension of the DeviceMesh

    :: N.B. If you pass a PG or a 1D list to perform a MPMD collective, the compiler won't be able to recover
    that information and perform collective algebraic optimization. Use other forms of input for that.
    """
    assert self.is_contiguous()
    tag, rankset, group_size = _expand_group(group, tag)
    tensor = torch._C._nn.all_gather_into_tensor(self, tag, rankset, group_size)  # type: ignore[attr-defined]
    res: torch.Tensor = AsyncCollectiveTensor(tensor)
    _register_wrapper_tensor(res, tensor)
    # TODO this should be done inside AsyncCollectiveTensor to delay the wait() call
    if gather_dim > 0:
        res = torch.cat(torch.chunk(res, group_size, dim=0), dim=gather_dim)
    return res

def reduce_scatter_tensor(
    self: torch.Tensor,
    reduceOp: str,
    scatter_dim: int,
    group: RANK_TYPES,
    tag: str = "",
):
    """
    Reduces the tensor data across all machines in such a way that all get
    the final result, then scatter the results to correponding ranks.


    The input tensor is left unmodified.
    Group can be one of:
        List[int]: ranks participating in the collective.
        List[List[int]]: 2D mesh of ranks taking part of this collective in MPMD.
        ProcessGroup: Will perform a collective using the ranks and tag of the PG.
        DeviceMesh: Do a SPMD collective over all ranks of the mesh
        (DeviceMesh, int): Do a MPMD collective over one dimension of the DeviceMesh
    :: N.B. If you pass a PG or a 1D list to perform a MPMD collective, the compiler won't be able to recover
    that information and perform collective algebraic optimization. Use other forms of input for that.
    """
    tag, rankset, group_size = _expand_group(group, tag)
    assert (
        self.size(scatter_dim) % group_size == 0
    ), f"input dimension 0 ({self.size(0)} must be a multiple of group_size {group_size}"
    if scatter_dim > 0:
        tensor_list = torch.chunk(self, group_size, dim=scatter_dim)
        self = torch.cat(tensor_list)

    tensor = torch._C._nn.reduce_scatter_tensor(self, reduceOp, tag, rankset, group_size)  # type: ignore[attr-defined]
    res = AsyncCollectiveTensor(tensor)
    _register_wrapper_tensor(res, tensor)
    return res


c10_lib_cpu = torch.library.Library("aten", "IMPL", "CPU")
c10_lib_cuda = torch.library.Library("aten", "IMPL", "CUDA")

def _register_ops():
    c10_lib_cpu.impl("all_reduce", _all_reduce)
    c10_lib_cuda.impl("all_reduce", _all_reduce)

    c10_lib_cpu.impl("wait_tensor", _wait_tensor)
    c10_lib_cuda.impl("wait_tensor", _wait_tensor)

    c10_lib_cpu.impl("all_gather_into_tensor", _all_gather_into_tensor)
    c10_lib_cuda.impl("all_gather_into_tensor", _all_gather_into_tensor)

    c10_lib_cpu.impl("reduce_scatter_tensor", _reduce_scatter_tensor)
    c10_lib_cuda.impl("reduce_scatter_tensor", _reduce_scatter_tensor)

if sys.executable != 'torch_deploy':
    _register_ops()
else:
    warnings.warn("PyTorch Distributed functional collectives do not work with torch::deploy.")
