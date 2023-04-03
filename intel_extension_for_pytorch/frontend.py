# coding: utf-8
import copy

import torch
import torch.fx.experimental.optimization as optimization
from torch.jit._trace import TracerWarning
import warnings
from enum import IntFlag

from .nn import utils
from .optim._optimizer_utils import optimizer_fusion, IPEX_FUSED_OPTIMIZER_LIST_CPU, IPEX_FUSED_OPTIMIZER_LIST_XPU
import intel_extension_for_pytorch._C as core
from .cpu.utils.channels_last_1d import to_channels_last_1d
from .cpu.utils.linear_bn_folding import linear_bn_fuse
from enum import IntEnum
from .cpu._auto_kernel_selection import _enable_dnnl, _disable_dnnl

from typing import List
import functools
import logging
import threading


def _copy_model_and_optimizer(model, optimizer):
    new_model = copy.deepcopy(model)
    if optimizer is None:
        return new_model, optimizer
    else:
        new_optimizer = copy.deepcopy(optimizer)
        new_optimizer.state.clear()
        dic_param = {}
        for k, value in zip(model.parameters(), new_model.parameters()):
            dic_param[k] = value

        # deep copy param_groups
        for group1, group2 in zip(optimizer.param_groups, new_optimizer.param_groups):
            for i, p in enumerate(group1['params']):
                # for the p not in the dic_param case, the new optimizer state will be updated
                # in _deep_copy_params_attr because the param here in optimizer state is the master
                # parameter of the model, which has ever optimized by ipex.optimize
                if p in dic_param:
                    new_model_param = dic_param[p]
                    group2['params'][i] = new_model_param
                    new_optimizer.state[new_model_param] = copy.deepcopy(optimizer.state[p])

        # deep copy params_attr for reentrancy of ipex.optimize
        def _deep_copy_params_attr(old_module, new_module):
            if hasattr(old_module, 'master_weight_split'):
                setattr(new_module, 'master_weight_split', old_module.master_weight_split)
                master_weight_split = getattr(new_module, 'master_weight_split')

                for name, param in old_module.named_parameters():
                    if master_weight_split:
                        attr_name = name + '_trail'
                        if param in optimizer.params_attr:
                            new_optimizer.params_attr[getattr(new_module, name)] = optimizer.params_attr[param]
                            new_optimizer.params_attr[getattr(new_module, name)]['trail'] = getattr(new_module, attr_name)
                    else:
                        attr_name = 'master_' + name
                        old_master_param = getattr(old_module, attr_name)
                        new_master_param = getattr(new_module, attr_name)
                        if old_master_param in optimizer.params_attr:
                            new_optimizer.params_attr[new_master_param] = optimizer.params_attr[old_master_param]
                            if 'bf16_param' in new_optimizer.params_attr[new_master_param]:
                                new_optimizer.params_attr[new_master_param]['bf16_param'] = getattr(new_module, name)
                            if 'fp16_param' in new_optimizer.params_attr[new_master_param]:
                                new_optimizer.params_attr[new_master_param]['fp16_param'] = getattr(new_module, name)

                        # deep copy new optimizer state for master parameter
                        new_optimizer.state[new_master_param] = copy.deepcopy(optimizer.state[old_master_param])

            for (_, old_child), (_, new_child) in zip(old_module.named_children(), new_module.named_children()):
                _deep_copy_params_attr(old_child, new_child)

        if hasattr(optimizer, 'params_attr'):
            params_attr = {}
            setattr(new_optimizer, 'params_attr', params_attr)
            _deep_copy_params_attr(model, new_model)

        return new_model, new_optimizer


# TODO: need discussion design here with CPU
class auto_channels_last_flag(IntFlag):
    AUTO = -1
    DISABLE = 0
    ENABLE = 1


auto_channels_last = auto_channels_last_flag.AUTO


def enable_auto_channels_last():
    global auto_channels_last
    auto_channels_last = auto_channels_last_flag.ENABLE


def disable_auto_channels_last():
    global auto_channels_last
    auto_channels_last = auto_channels_last_flag.DISABLE


class _Properties(object):
    r"""
    This class is to establish a set of default properties.

    """

    def __init__(self):
        self.opt_level = None
        self.conv_bn_folding = None
        self.weights_prepack = None
        self.remove_dropout = None
        # optimizer opt conig
        self.split_master_weight_for_bf16 = None
        self.fuse_update_step = None
        self.auto_kernel_selection = None
        self.graph_mode = None


# O0 properties
class _O0:
    def __call__(self, properties):
        properties.opt_level = "O0"
        properties.conv_bn_folding = False
        properties.linear_bn_folding = False
        properties.weights_prepack = False
        properties.replace_dropout_with_identity = False
        properties.optimize_lstm = False
        properties.split_master_weight_for_bf16 = False
        properties.fuse_update_step = False
        properties.auto_kernel_selection = False
        properties.graph_mode = False
        return properties


# O1 properties
class _O1:
    def __call__(self, properties):
        properties.opt_level = "O1"
        properties.conv_bn_folding = True
        properties.linear_bn_folding = True
        properties.weights_prepack = True
        properties.replace_dropout_with_identity = True
        properties.optimize_lstm = True
        properties.split_master_weight_for_bf16 = True
        properties.fuse_update_step = True
        properties.auto_kernel_selection = False
        properties.graph_mode = False
        return properties


opt_levels = {"O0": _O0(),
              "O1": _O1()}


class RunMethods(IntEnum):
    JIT = 1
    TorchDynamo = 2
    EagerInfer = 3
    EagerTrain = 4


class GraphCapture(object):

    def __init__(self, model, train, dtype, weights_prepack):
        self.model = copy.deepcopy(model)
        self.train = train
        self.dtype = dtype
        self.weights_prepack = weights_prepack
        self.method = None
        self.lock = threading.Lock()

    def __call__(self, func):

        def compiler(gm: torch.fx.GraphModule, example_inputs: List[torch.Tensor]):
            traced_gm = torch.jit.trace(gm.eval(), example_inputs).eval()
            traced_gm = torch.jit.freeze(traced_gm)
            return traced_gm

        @functools.wraps(func)
        def forward(*input, **kwargs):
            if torch.jit.is_tracing():
                return func(*input, **kwargs)
            with torch.cpu.amp.autocast(enabled=(self.dtype == torch.bfloat16 or self.dtype == torch.half), dtype=self.dtype):
                if self.method:
                    if self.train:
                        return func(*input, **kwargs)
                    else:
                        return self.model(*input, **kwargs)
                else:
                    # Lock the graph generation process to avoid multiple threads generating graph simultaneously.
                    with self.lock:
                        if self.method:
                            if self.train:
                                return func(*input, **kwargs)
                            else:
                                return self.model(*input, **kwargs)
                        if self.train:
                            warnings.warn("graph capture does not support training yet.")
                            self.method = RunMethods.EagerTrain
                            return func(*input, **kwargs)
                        else:
                            try:
                                # Try JIT trace.
                                # Tracing only records operations done when the given function is run on the given tensors.
                                # Therefore, the returned ScriptModule will always run the same traced graph on any input.
                                # This has some important implications when your module is expected to run different
                                # sets of operations, depending on the input and/or the module state. In cases like these,
                                # tracing would not be appropriate, and the tracer will try to emit warnings when doing
                                # something that may cause an incorrect trace to be produced. Therefore, we catch these
                                # warnings and treat them as errors, and let TorchDynamo handle such models appropriately.
                                with warnings.catch_warnings():
                                    warnings.filterwarnings('error', category=TracerWarning)
                                    traced_model = torch.jit.trace(self.model.eval(), input).eval()
                                    traced_model = torch.jit.freeze(traced_model)
                                    output = traced_model(*input, **kwargs)
                                    self.model = traced_model
                                    self.method = RunMethods.JIT
                                    logging.debug("generate graph by JIT trace.")
                                    return output
                            except BaseException:
                                try:
                                    # JIT trace failed, try torchdynamo with JIT trace backend.
                                    import torchdynamo
                                    torchdynamo.reset()
                                    torchdynamo.config.dynamic_shapes = True
                                    dynamo_model = torchdynamo.optimize(compiler)(self.model)
                                    output = dynamo_model(*input, **kwargs)
                                    self.model = dynamo_model
                                    self.method = RunMethods.TorchDynamo
                                    logging.debug("generate graph by TorchDynamo.")
                                    return output
                                except BaseException:
                                    warnings.warn("Both JIT and TorchDynamo failed, fallback to original model.")
                                    self.method = RunMethods.EagerInfer
                                    if self.weights_prepack:
                                        if self.dtype == torch.bfloat16:
                                            assert core.onednn_has_bf16_support(), \
                                                "BF16 weight prepack needs the cpu support avx512bw, avx512vl and avx512dq, " + \
                                                "please set dtype to torch.float or set weights_prepack to False."
                                        if self.dtype == torch.half:
                                            assert core.onednn_has_fp16_support(), \
                                                "FP16 weight prepack needs the cpu support avx512_core_fp16, " + \
                                                "please set dtype to torch.float or set weights_prepack to False."
                                        self.model, _, _ = utils._weight_prepack.weight_prepack_with_ipex(
                                            self.model, None, {})
                                    return self.model(*input, **kwargs)

        return forward


def optimize(
    model,
    dtype=None,
    optimizer=None,
    level="O1",
    inplace=False,
    conv_bn_folding=None,
    linear_bn_folding=None,
    weights_prepack=None,
    replace_dropout_with_identity=None,
    optimize_lstm=None,
    split_master_weight_for_bf16=None,
    fuse_update_step=None,
    auto_kernel_selection=None,
    sample_input=None,
    graph_mode=None
):
    r"""
    Apply optimizations at Python frontend to the given model (nn.Module), as
    well as the given optimizer (optional). If the optimizer is given,
    optimizations will be applied for training. Otherwise, optimization will be
    applied for inference. Optimizations include ``conv+bn`` folding (for
    inference only), weight prepacking and so on.

    Weight prepacking is a technique to accelerate performance of oneDNN
    operators. In order to achieve better vectorization and cache reuse, onednn
    uses a specific memory layout called ``blocked layout``. Although the
    calculation itself with ``blocked layout`` is fast enough, from memory usage
    perspective it has drawbacks. Running with the ``blocked layout``, oneDNN
    splits one or several dimensions of data into blocks with fixed size each
    time the operator is executed. More details information about oneDNN data
    mermory format is available at `oneDNN manual
    <https://oneapi-src.github.io/oneDNN/dev_guide_understanding_memory_formats.html>`_.
    To reduce this overhead, data will be converted to predefined block shapes
    prior to the execution of oneDNN operator execution. In runtime, if the data
    shape matches oneDNN operator execution requirements, oneDNN won't perform
    memory layout conversion but directly go to calculation. Through this
    methodology, called ``weight prepacking``, it is possible to avoid runtime
    weight data format convertion and thus increase performance.

    Args:
        model (torch.nn.Module): User model to apply optimizations on.
        dtype (torch.dtype): Only works for ``torch.bfloat16`` and ``torch.half`` a.k.a ``torch.float16``.
            Model parameters will be casted to ``torch.bfloat16`` or ``torch.half``
            according to dtype of settings. The default value is None, meaning do nothing.
            Note: Data type conversion is only applied to ``nn.Conv2d``, ``nn.Linear``
            and ``nn.ConvTranspose2d`` for both training and inference cases. For
            inference mode, additional data type conversion is applied to the weights
            of ``nn.Embedding`` and ``nn.LSTM``.
        optimizer (torch.optim.Optimizer): User optimizer to apply optimizations
            on, such as SGD. The default value is ``None``, meaning inference case.
        level (string): ``"O0"`` or ``"O1"``. No optimizations are applied with
            ``"O0"``. The optimizer function just returns the original model and
            optimizer. With ``"O1"``, the following optimizations are applied:
            conv+bn folding, weights prepack, dropout removal (inferenc model),
            master weight split and fused optimizer update step (training model).
            The optimization options can be further overridden by setting the
            following options explicitly. The default value is ``"O1"``.
        inplace (bool): Whether to perform inplace optimization. Default value is
            ``False``.
        conv_bn_folding (bool): Whether to perform ``conv_bn`` folding. It only
            works for inference model. The default value is ``None``. Explicitly
            setting this knob overwrites the configuration set by ``level`` knob.
        linear_bn_folding (bool): Whether to perform ``linear_bn`` folding. It only
            works for inference model. The default value is ``None``. Explicitly
            setting this knob overwrites the configuration set by ``level`` knob.
        weights_prepack (bool): Whether to perform weight prepack for convolution
            and linear to avoid oneDNN weights reorder. The default value is
            ``None``. Explicitly setting this knob overwrites the configuration
            set by ``level`` knob. For now, XPU doesn't support weights prepack.
        replace_dropout_with_identity (bool): Whether to replace ``nn.Dropout``
            with ``nn.Identity``. If replaced, the ``aten::dropout`` won't be
            included in the JIT graph. This may provide more fusion opportunites
            on the graph. This only works for inference model. The default value
            is ``None``. Explicitly setting this knob overwrites the configuration
            set by ``level`` knob.
        optimize_lstm (bool): Whether to replace ``nn.LSTM`` with ``IPEX LSTM``
            which takes advantage of oneDNN kernels to get better performance.
            The default value is ``None``. Explicitly setting this knob
            overwrites the configuration set by ``level`` knob.
        split_master_weight_for_bf16 (bool): Whether to split master weights
            update for BF16 training. This saves memory comparing to master
            weight update solution. Split master weights update methodology
            doesn't support all optimizers. The default value is None. The
            default value is ``None``. Explicitly setting this knob overwrites
            the configuration set by ``level`` knob.
        fuse_update_step (bool): Whether to use fused params update for training
            which have better performance. It doesn't support all optimizers.
            The default value is ``None``. Explicitly setting this knob
            overwrites the configuration set by ``level`` knob.
        sample_input (tuple or torch.Tensor): Whether to feed sample input data to ipex.optimize. The shape of
            input data will impact the block format of packed weight. If not feed a sample
            input, Intel® Extension for PyTorch* will pack the weight per some predefined heuristics.
            If feed a sample input with real input shape, Intel® Extension for PyTorch* can get
            best block format.
        auto_kernel_selection (bool) [experimental]: Different backends may have
            different performances with different dtypes/shapes. Default value
            is False. Intel® Extension for PyTorch* will try to optimize the
            kernel selection for better performance if this knob is set to
            ``True``. You might get better performance at the cost of extra memory usage.
            The default value is ``None``. Explicitly setting this knob overwrites the
            configuration set by ``level`` knob.
        graph_mode: (bool) [experimental]: It will automatically apply a combination of methods
            to generate graph or multiple subgraphs if True. The default value is ``False``.

    Returns:
        Model and optimizer (if given) modified according to the ``level`` knob
        or other user settings. ``conv+bn`` folding may take place and
        ``dropout`` may be replaced by ``identity``. In inference scenarios,
        convolutuon, linear and lstm will be replaced with the optimized
        counterparts in Intel® Extension for PyTorch* (weight prepack for
        convolution and linear) for good performance. In bfloat16 or float16 scenarios,
        parameters of convolution and linear will be casted to bfloat16 or float16 dtype.

    .. warning::

        Please invoke ``optimize`` function BEFORE invoking DDP in distributed
        training scenario.

        The ``optimize`` function deepcopys the original model. If DDP is invoked
        before ``optimize`` function, DDP is applied on the origin model, rather
        than the one returned from ``optimize`` function. In this case, some
        operators in DDP, like allreduce, will not be invoked and thus may cause
        unpredictable accuracy loss.

    Examples:

        >>> # bfloat16 inference case.
        >>> model = ...
        >>> model.load_state_dict(torch.load(PATH))
        >>> model.eval()
        >>> optimized_model = ipex.optimize(model, dtype=torch.bfloat16)
        >>> # running evaluation step.
        >>> # bfloat16 training case.
        >>> optimizer = ...
        >>> model.train()
        >>> optimized_model, optimized_optimizer = ipex.optimize(model, dtype=torch.bfloat16, optimizer=optimizer)
        >>> # running training step.

    `torch.xpu.optimize()` is an alternative of optimize API in Intel® Extension for PyTorch*,
    to provide identical usage for XPU device only. The motivation of adding this alias is
    to unify the coding style in user scripts base on torch.xpu modular.

    Examples:

        >>> # bfloat16 inference case.
        >>> model = ...
        >>> model.load_state_dict(torch.load(PATH))
        >>> model.eval()
        >>> optimized_model = torch.xpu.optimize(model, dtype=torch.bfloat16)
        >>> # running evaluation step.
        >>> # bfloat16 training case.
        >>> optimizer = ...
        >>> model.train()
        >>> optimized_model, optimized_optimizer = torch.xpu.optimize(model, dtype=torch.bfloat16, optimizer=optimizer)
        >>> # running training step.

    """
    if isinstance(model, torch.jit.ScriptModule):
        if optimizer is None:
            return model
        return model, optimizer

    if model.training:
        assert optimizer is not None, "The optimizer should be given for training mode"
    else:
        assert optimizer is None, "The optimizer should not be given for inference mode"

    opt_properties = _Properties()
    if level not in opt_levels:
        raise RuntimeError(
            "Unexpected optimization level {}. ".format(level)
            + "Options are 'O0', 'O1'.")
    else:
        opt_properties = opt_levels[level](opt_properties)

    device_type = 'cpu'
    model_parameters_list = list(model.parameters())
    if len(model_parameters_list) and model_parameters_list[0].device.type == 'xpu':
        if not all([param.device.type == 'xpu' for param in model_parameters_list]):
            raise RuntimeError("The model is mixed with different device type")
        else:
            device_type = 'xpu'

    global auto_channels_last

    def xpu_check_channel_last():
        global auto_channels_last
        if auto_channels_last.value == auto_channels_last_flag.ENABLE:
            return True
        elif auto_channels_last.value == auto_channels_last_flag.AUTO and torch.xpu.has_2d_block_array():
            return True
        else:
            return False

    if device_type == 'cpu' and (auto_channels_last.value != auto_channels_last_flag.DISABLE):
        _convert_convNd_deconvNd_weight_memory_format(model)
    elif device_type == 'xpu' and xpu_check_channel_last():
        _convert_convNd_deconvNd_weight_memory_format(model)

    if level is not None:
        opt_properties.opt_level = level
    if conv_bn_folding is not None:
        opt_properties.conv_bn_folding = conv_bn_folding
    if linear_bn_folding is not None:
        opt_properties.linear_bn_folding = linear_bn_folding
    if weights_prepack is not None:
        opt_properties.weights_prepack = weights_prepack
    if replace_dropout_with_identity is not None:
        opt_properties.replace_dropout_with_identity = replace_dropout_with_identity
    if optimize_lstm is not None:
        opt_properties.optimize_lstm = optimize_lstm
    if split_master_weight_for_bf16 is not None:
        opt_properties.split_master_weight_for_bf16 = split_master_weight_for_bf16
    if fuse_update_step is not None:
        opt_properties.fuse_update_step = fuse_update_step
    if auto_kernel_selection is not None:
        opt_properties.auto_kernel_selection = auto_kernel_selection
    if graph_mode is not None:
        opt_properties.graph_mode = graph_mode

    _disable_dnnl()
    if opt_properties.auto_kernel_selection:
        _enable_dnnl()

    # when on xpu, some features are not supported
    if device_type == 'xpu':
        if opt_properties.auto_kernel_selection:
            warnings.warn("Auto Kernel Selection feature is not supported on XPU, disabled.")
            opt_properties.auto_kernel_selection = False
        if opt_properties.split_master_weight_for_bf16:
            warnings.warn("Split Master Weight feature is not supported on XPU for now, disabled.")
            # TODO: for xpu, the split master weight will be supported soon
            opt_properties.split_master_weight_for_bf16 = False
        if opt_properties.graph_mode:
            warnings.warn(
                "Out-of-Box (OOB) solution for inference supposes to trace a model outside "
                + "the torch.xpu.optimize on XPU, disabled the graph mode.")
            # TODO: for xpu now, the oob solution for inference is to trace model outside of the torch.xpu.optimize.
            opt_properties.graph_mode = False
        if not inplace:
            warnings.warn(
                "To reduce device memory usage on XPU, optimization are done inplace,"
                + " setting the inplace argument to True.")
            # TODO: for xpu, inplace is true will add device memory pressure, so set inplace to be true
            inplace = True
        if opt_properties.weights_prepack or sample_input is not None:
            warnings.warn(
                "Weight Prepack and Sample Input are both disabled on XPU. The Onednn Layout"
                + " is automatically applied.")
            opt_properties.weights_prepack = False
            sample_input = None

    if inplace:
        optimized_model = model
        optimized_optimizer = optimizer
    else:
        optimized_model, optimized_optimizer = _copy_model_and_optimizer(model, optimizer)

    if sample_input is not None:
        if isinstance(sample_input, torch.Tensor):
            sample_input = (sample_input,)
        utils._weight_prepack.record_input_shape_for_prepack(optimized_model, sample_input)

    if not model.training:
        if opt_properties.conv_bn_folding:
            try:
                optimized_model = optimization.fuse(optimized_model, inplace=inplace)
            except:  # noqa E722
                warnings.warn("Conv BatchNorm folding failed during the optimize process.")
        if opt_properties.linear_bn_folding:
            try:
                optimized_model = linear_bn_fuse(optimized_model, inplace=inplace)
            except: # noqa E722
                warnings.warn("Linear BatchNorm folding failed during the optimize process.")
        if opt_properties.replace_dropout_with_identity:
            utils._model_convert.replace_dropout_with_identity(optimized_model)
        if dtype == torch.bfloat16:
            optimized_model = utils._model_convert.convert_module_data_type(optimized_model, torch.bfloat16)
        if dtype == torch.half:
            optimized_model = utils._model_convert.convert_module_data_type(optimized_model, torch.half)

    if opt_properties.optimize_lstm:
        utils._model_convert.replace_lstm_with_ipex_lstm(optimized_model, optimized_optimizer)
    if model.training and opt_properties.split_master_weight_for_bf16 and dtype is torch.bfloat16:
        if not opt_properties.fuse_update_step:
            opt_properties.split_master_weight_for_bf16 = False
            warnings.warn(
                "IPEX does not support non-fused split master weight for bf16 training, "
                + "reset split_master_weight_for_bf16 flag to False. "
                + "If you want to use split_master_weight_for_bf16, "
                + "please set both split_master_weight_for_bf16 and fuse_update_step to True.")
        elif type(optimizer) not in IPEX_FUSED_OPTIMIZER_LIST_CPU and device_type == 'cpu':
            opt_properties.split_master_weight_for_bf16 = False
            opt_properties.fuse_update_step = False
            warnings.warn(
                "IPEX CPU does not support fused/fused split update for " + str(type(optimizer))
                + ", use non-fused master weight update for bf16 training on CPU.")
        elif type(optimizer) not in IPEX_FUSED_OPTIMIZER_LIST_XPU and device_type == 'xpu':
            opt_properties.split_master_weight_for_bf16 = False
            opt_properties.fuse_update_step = False
            warnings.warn(
                "IPEX XPU does not support fused/fused split update for " + str(type(optimizer))
                + ", use non-fused master weight update for bf16 training on XPU.")

    # convert optimizer for training case.
    params_attr = {}
    if hasattr(optimized_optimizer, 'params_attr'):
        params_attr = optimized_optimizer.params_attr
    if dtype == torch.bfloat16 and model.training:
        optimized_model, optimized_optimizer, params_attr = utils._weight_cast.weight_dtype_convert_with_ipex(
            optimized_model, optimized_optimizer, params_attr, opt_properties.split_master_weight_for_bf16,
            convert_dtype=torch.bfloat16)
    if dtype == torch.half and model.training:
        assert device_type != 'xpu', "For now, XPU device does not support model training with half precision."
        optimized_model, optimized_optimizer, params_attr = utils._weight_cast.weight_dtype_convert_with_ipex(
            optimized_model, optimized_optimizer, params_attr, False, convert_dtype=torch.half)
    # Since TorchDynamo cannot handle custom operations yet, for the case of inference graph mode,
    # the weights prepacking here is temporarily cancelled, and it will be completed on the graph.
    if opt_properties.weights_prepack:
        if device_type == 'cpu':
            if opt_properties.graph_mode is not True or optimizer is not None:
                if dtype == torch.bfloat16:
                    assert core.onednn_has_bf16_support(), \
                        "BF16 weight prepack needs the cpu support avx512bw, avx512vl and avx512dq, " + \
                        "please set dtype to torch.float or set weights_prepack to False."
                if dtype == torch.half:
                    assert core.onednn_has_fp16_support(), \
                        "FP16 weight prepack needs the cpu support avx512_core_fp16, " + \
                        "please set dtype to torch.float or set weights_prepack to False."
                optimized_model, optimized_optimizer, params_attr = utils._weight_prepack.weight_prepack_with_ipex(
                    optimized_model, optimized_optimizer, params_attr, 'cpu')

    if opt_properties.graph_mode:
        _old_forward = optimized_model.forward
        wrapper = GraphCapture(optimized_model, optimizer is not None, dtype, opt_properties.weights_prepack)
        optimized_model.forward = wrapper(_old_forward)

    # TODO: model list, optimizer list.
    if optimizer is None:
        return optimized_model

    # with an optimizer
    if opt_properties.fuse_update_step:
        optimized_optimizer = optimizer_fusion(
            optimized_optimizer, opt_properties.split_master_weight_for_bf16, device_type)
    return optimized_model, optimized_optimizer


def enable_onednn_fusion(enabled):
    r"""
    Enables or disables oneDNN fusion functionality. If enabled, oneDNN
    operators will be fused in runtime, when intel_extension_for_pytorch
    is imported.

    Args:
        enabled (bool): Whether to enable oneDNN fusion functionality or not.
            Default value is ``True``.

    Examples:

        >>> import intel_extension_for_pytorch as ipex
        >>> # to enable the oneDNN fusion
        >>> ipex.enable_onednn_fusion(True)
        >>> # to disable the oneDNN fusion
        >>> ipex.enable_onednn_fusion(False)
    """

    if enabled:
        core.enable_jit_opt()
    else:
        core.disable_jit_opt()


def _convert_convNd_deconvNd_weight_memory_format(module):
    # inspired from https://github.com/pytorch/pytorch/blob/master/torch/nn/utils/memory_format.py
    if isinstance(module, (torch.nn.Conv1d, torch.nn.ConvTranspose1d)):
        weight_data = to_channels_last_1d(module.weight.detach().clone())
        module.weight.data = weight_data.resize_(weight_data.size())
    elif isinstance(module, (torch.nn.Conv2d, torch.nn.ConvTranspose2d)):
        weight_data = module.weight.detach().clone().contiguous(memory_format=torch.channels_last)
        module.weight.data = weight_data.resize_(weight_data.size(), memory_format=torch.channels_last)
    elif isinstance(module, (torch.nn.Conv3d, torch.nn.ConvTranspose3d)):
        weight_data = module.weight.detach().clone().contiguous(memory_format=torch.channels_last_3d)
        module.weight.data = weight_data.resize_(weight_data.size(), memory_format=torch.channels_last_3d)

    for child in module.children():
        _convert_convNd_deconvNd_weight_memory_format(child)


class FP32MathMode(IntEnum):
    FP32 = int(core.FP32MathMode.FP32)
    TF32 = int(core.FP32MathMode.TF32)
    BF32 = int(core.FP32MathMode.BF32)


def set_fp32_math_mode(mode=FP32MathMode.FP32, device="cpu"):
    r"""
    Enable or disable implicit data type conversion.

    Args:
        mode (FP32MathMode): ``FP32MathMode.FP32``, ``FP32MathMode.BF32`` or
            ``FP32MathMode.TF32`` (GPU ONLY). oneDNN fpmath mode will be disabled by default if dtype
            is set to ``FP32MathMode.FP32``. The implicit ``FP32`` to ``TF32`` data type conversion
            will be enabled if dtype is set to ``FP32MathMode.TF32``. The implicit ``FP32``
            to ``BF16`` data type conversion will be enabled if dtype is set to ``FP32MathMode.BF32``.
        device (string): ``cpu``, ``xpu``

    Examples:

        >>> import intel_extension_for_pytorch as ipex
        >>> # to enable the implicit data type conversion
        >>> ipex.set_fp32_math_mode(device="xpu", mode=ipex.FP32MathMode.BF32)
        >>> # to disable the implicit data type conversion
        >>> ipex.set_fp32_math_mode(device="xpu", mode=ipex.FP32MathMode.FP32)

    ``torch.xpu.set_fp32_math_mode()`` is an alternative function in Intel® Extension for PyTorch*,
    to provide identical usage for XPU device only. The motivation of adding this alias is
    to unify the coding style in user scripts base on ``torch.xpu`` modular.

    Examples:

        >>> import intel_extension_for_pytorch as ipex
        >>> # to enable the implicit data type conversion
        >>> torch.xpu.set_fp32_math_mode(device="xpu", mode=ipex.FP32MathMode.BF32)
        >>> # to disable the implicit data type conversion
        >>> torch.xpu.set_fp32_math_mode(device="xpu", mode=ipex.FP32MathMode.FP32)
    """

    if device == "cpu":
        if mode == FP32MathMode.BF32:
            core.set_fp32_math_mode(core.FP32MathMode.BF32)
        elif mode == FP32MathMode.FP32:
            core.set_fp32_math_mode(core.FP32MathMode.FP32)
        else:
            warnings.warn(
                "For CPU device, IPEX does not support mode except \
                    FP32MathMode.FP32 and FP32MathMode.BF32 for fpmath_mode right now.")
    elif device == "xpu":
        if mode == FP32MathMode.BF32:
            torch.xpu.set_fp32_math_mode(torch.xpu.FP32MathMode.BF32)
        elif mode == FP32MathMode.FP32:
            torch.xpu.set_fp32_math_mode(torch.xpu.FP32MathMode.FP32)
        elif mode == FP32MathMode.TF32:
            torch.xpu.set_fp32_math_mode(torch.xpu.FP32MathMode.TF32)
        else:
            warnings.warn(
                "For XPU device, IPEX does not support mode except \
                    FP32MathMode.FP32, FP32MathMode.BF32 and FP32MathMode.TF32 for fpmath_mode right now.")
    else:
        raise RuntimeError("Unexpected device type {}. ".format(device) + "Supported are 'cpu', 'xpu'.")


def get_fp32_math_mode(device="cpu"):
    r"""
    Get the current fpmath_mode setting.

    Args:
        device (string): ``cpu``, ``xpu``

    Returns:
        Fpmath mode
        The value will be ``FP32MathMode.FP32``, ``FP32MathMode.BF32`` or ``FP32MathMode.TF32`` (GPU ONLY).
        oneDNN fpmath mode will be disabled by default if dtype is set to ``FP32MathMode.FP32``.
        The implicit ``FP32`` to ``TF32`` data type conversion will be enabled if dtype is set
        to ``FP32MathMode.TF32``. The implicit ``FP32`` to ``BF16`` data type conversion will be
        enabled if dtype is set to ``FP32MathMode.BF32``.

    Examples:

        >>> import intel_extension_for_pytorch as ipex
        >>> # to get the current fpmath mode
        >>> ipex.get_fp32_math_mode(device="xpu")

    ``torch.xpu.get_fp32_math_mode()`` is an alternative function in Intel® Extension for PyTorch*,
    to provide identical usage for XPU device only. The motivation of adding this alias is
    to unify the coding style in user scripts base on ``torch.xpu`` modular.

    Examples:

        >>> import intel_extension_for_pytorch as ipex
        >>> # to get the current fpmath mode
        >>> torch.xpu.get_fp32_math_mode(device="xpu")
    """

    if device == "cpu":
        return core.get_fp32_math_mode()
    elif device == "xpu":
        return torch.xpu.get_fp32_math_mode()
    else:
        raise RuntimeError("Unexpected device type {}. ".format(device) + "Supported are 'cpu', 'xpu'.")
